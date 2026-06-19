"""Intent-named repositories — the tenant-scoped boundary over the ORM.

Every relational read/write goes through one of these (ADR-0004: ``db/`` is the
only SQL). Each repository is **constructed with a resolved tenant scope** and a
``Session``; it applies the ``tenant_id`` predicate to every query it issues
(spec 0004 §2.1, INV-1) and returns **domain entities** — never an ORM row, a
``Session``, or vendor types (backend/AGENTS.md). The tenant comes from
``auth/`` upstream, never from request-body input.

INV-1 is structural here, not by convention: there is no method that omits the
tenant predicate, and the tenant id is bound once at construction, so a caller
in tenant A *cannot* phrase a query that reaches tenant B's rows — a lookup of a
foreign-tenant id returns ``None`` / no rows (the negative test asserts exactly
this). Existence non-disclosure (404, not 403) is enforced one layer up, in the
service/api boundary, off these ``None`` returns.

Repositories persist via the session but **do not commit**; the caller owns the
transaction boundary (request handler / ``session_scope``), so a use-case that
touches several repositories commits atomically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.entities import (
    AuditEvent,
    AuditOutcome,
    Chunk,
    Citation,
    Collection,
    Document,
    DocumentStatus,
    Message,
    MessageRole,
    RefreshToken,
    Role,
    Tenant,
    User,
)
from app.domain.entities import ChatSession as ChatSessionEntity

# ---------------------------------------------------------------------------
# Row → domain mappers (the boundary: ORM rows never escape this module).
# ---------------------------------------------------------------------------


def _to_tenant(row: models.Tenant) -> Tenant:
    return Tenant(id=row.id, name=row.name, created_at=row.created_at, updated_at=row.updated_at)


def _to_user(row: models.User) -> User:
    return User(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        password_hash=row.password_hash,
        roles=tuple(Role(r) for r in row.roles),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_refresh_token(row: models.RefreshToken) -> RefreshToken:
    return RefreshToken(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


def _to_collection(row: models.Collection) -> Collection:
    return Collection(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_document(row: models.Document) -> Document:
    return Document(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        collection_id=row.collection_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        status=DocumentStatus(row.status),
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_chunk(row: models.Chunk) -> Chunk:
    return Chunk(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        ord=row.ord,
        text=row.text,
        embedding=tuple(row.embedding) if row.embedding is not None else None,
        char_start=row.char_start,
        char_end=row.char_end,
        created_at=row.created_at,
    )


def _to_chat_session(row: models.ChatSession) -> ChatSessionEntity:
    return ChatSessionEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        title=row.title,
        model=row.model,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_message(row: models.Message) -> Message:
    return Message(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=MessageRole(row.role),
        content=row.content,
        model=row.model,
        created_at=row.created_at,
    )


def _to_citation(row: models.Citation) -> Citation:
    return Citation(
        id=row.id,
        tenant_id=row.tenant_id,
        message_id=row.message_id,
        chunk_id=row.chunk_id,
        char_start=row.char_start,
        char_end=row.char_end,
        score=row.score,
        created_at=row.created_at,
    )


def _to_audit_event(row: models.AuditEvent) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        outcome=AuditOutcome(row.outcome),
        request_id=row.request_id,
        source_ip=row.source_ip,
        metadata=dict(row.event_metadata),
        ts=row.ts,
    )


class _TenantScopedRepository:
    """Base for repositories bound to one tenant.

    The tenant id is captured at construction and applied to *every* query.
    Subclasses never expose a method that omits it. The session is held but
    never returned (no ``Session`` leaks upward, backend/AGENTS.md).
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> UUID:
        """The tenant this repository is scoped to (read-only)."""
        return self._tenant_id


class TenantRepository:
    """The one repository that is **not** tenant-scoped — it owns ``tenants``.

    A tenant has no parent tenant, so this takes only a session. It exists so
    bootstrapping/provisioning still goes through ``db/`` rather than raw SQL.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, name: str) -> Tenant:
        row = models.Tenant(name=name)
        self._session.add(row)
        await self._session.flush()
        return _to_tenant(row)

    async def get(self, tenant_id: UUID) -> Tenant | None:
        row = await self._session.get(models.Tenant, tenant_id)
        return _to_tenant(row) if row is not None else None


class UserLookupRepository:
    """The one **non**-tenant-scoped user lookup — the pre-identity step (CC-3).

    Login arrives with no tenant (the client never sends one, spec 0004 §2.3),
    so resolving *which* tenant an email belongs to must precede tenant scoping.
    This is that single, deliberate exception: a tenant-agnostic lookup by email
    or id, used **only** by ``auth``/the auth service. Email is unique within a
    tenant; for the MVP (one principal → one tenant) it is effectively unique
    globally, so this returns at most one user. Every operation *after* identity
    resolution goes through the tenant-scoped :class:`UserRepository`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_email(self, email: str) -> User | None:
        stmt = select(models.User).where(models.User.email == email)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def find_refresh_token_owner(self, token_hash: str) -> RefreshToken | None:
        """Resolve a refresh-token row by hash, tenant-agnostically (refresh path).

        The refresh cookie carries no tenant; the hash is globally unique, so
        this finds the owning row, after which the caller scopes to its tenant.
        """
        stmt = select(models.RefreshToken).where(models.RefreshToken.token_hash == token_hash)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_refresh_token(row) if row is not None else None


class UserRepository(_TenantScopedRepository):
    """Users within one tenant (spec 0004 §2.3)."""

    async def create(self, *, email: str, password_hash: str, roles: Sequence[Role]) -> User:
        row = models.User(
            tenant_id=self._tenant_id,
            email=email,
            password_hash=password_hash,
            roles=[r.value for r in roles],
        )
        self._session.add(row)
        await self._session.flush()
        return _to_user(row)

    async def get(self, user_id: UUID) -> User | None:
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.id == user_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.email == email,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None


class RefreshTokenRepository(_TenantScopedRepository):
    """Rotating, revocable refresh tokens within one tenant (spec 0004 §2.3).

    Only the token **hash** is ever passed in/out — the opaque token is hashed
    in ``auth/`` before it reaches here. Lookups are tenant-scoped (INV-1) so a
    token minted in tenant A can never be resolved by a tenant-B repository.
    """

    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        row = models.RefreshToken(
            tenant_id=self._tenant_id,
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_refresh_token(row)

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(models.RefreshToken).where(
            models.RefreshToken.tenant_id == self._tenant_id,
            models.RefreshToken.token_hash == token_hash,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_refresh_token(row) if row is not None else None

    async def revoke(self, token_hash: str) -> bool:
        """Mark a token revoked (idempotent). Returns False if not found here."""
        stmt = select(models.RefreshToken).where(
            models.RefreshToken.tenant_id == self._tenant_id,
            models.RefreshToken.token_hash == token_hash,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await self._session.flush()
        return True


class CollectionRepository(_TenantScopedRepository):
    """Collections within one tenant."""

    async def create(
        self, *, owner_id: UUID, name: str, description: str | None = None
    ) -> Collection:
        row = models.Collection(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            description=description,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_collection(row)

    async def get(self, collection_id: UUID) -> Collection | None:
        stmt = select(models.Collection).where(
            models.Collection.tenant_id == self._tenant_id,
            models.Collection.id == collection_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_collection(row) if row is not None else None

    async def list_for_owner(self, owner_id: UUID) -> list[Collection]:
        stmt = (
            select(models.Collection)
            .where(
                models.Collection.tenant_id == self._tenant_id,
                models.Collection.owner_id == owner_id,
            )
            .order_by(models.Collection.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_collection(r) for r in rows]

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[Collection]:
        """A keyset page of an owner's collections (newest first).

        Owner-scoped *and* tenant-scoped (deny-by-default ownership, spec 0004
        §2.2 + INV-1): a caller only ever sees their own. Ordered by
        ``(created_at, id)`` **descending** — ``id`` is the stable tiebreaker so
        the total order is deterministic even when two rows share a timestamp.
        ``after_id`` is the id of the last row of the previous page (the decoded
        cursor); rows strictly *after* it in that order are returned. At most
        ``limit`` rows come back; the caller decides if more remain.

        The keyset boundary is the cursor row's ``(created_at, id)`` resolved by a
        **correlated scalar subquery** rather than a client-supplied timestamp.
        This is deliberate: comparing column-to-column inside the database is
        exact on Postgres *and* on the SQLite used by the offline tests, whereas
        re-binding a Python ``datetime`` would hit SQLite's lossy ``DateTime``
        round-trip (sub-second precision dropped, tz rendering divergent) and
        leak duplicates across pages. The cursor therefore carries only the id.
        """
        conditions = [
            models.Collection.tenant_id == self._tenant_id,
            models.Collection.owner_id == owner_id,
        ]
        if after_id is not None:
            # The cursor row's created_at, resolved in-DB (no Python datetime
            # crosses the boundary). Scoped to this tenant so a foreign cursor id
            # resolves to NULL and the keyset predicate excludes everything —
            # fail-closed rather than leaking another tenant's ordering.
            boundary_created_at = (
                select(models.Collection.created_at)
                .where(
                    models.Collection.tenant_id == self._tenant_id,
                    models.Collection.id == after_id,
                )
                .scalar_subquery()
            )
            # Keyset predicate for DESC ordering by (created_at, id): the next
            # page is rows whose (created_at, id) sorts strictly after the cursor.
            conditions.append(
                or_(
                    models.Collection.created_at < boundary_created_at,
                    and_(
                        models.Collection.created_at == boundary_created_at,
                        models.Collection.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Collection)
            .where(*conditions)
            .order_by(models.Collection.created_at.desc(), models.Collection.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_collection(r) for r in rows]

    async def count_documents(self, collection_id: UUID) -> int:
        """Count documents contained in a collection (the ``document_count``).

        Tenant-scoped on both the collection and its documents (INV-1). Returns
        ``0`` for an empty or non-existent collection — the service has already
        established visibility before asking.
        """
        stmt = (
            select(func.count())
            .select_from(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.collection_id == collection_id,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def update(
        self,
        collection_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        set_description: bool = False,
    ) -> Collection | None:
        """Apply a partial update to a collection (tenant-scoped).

        Only the fields the caller supplied are touched. ``name`` is updated when
        non-``None``. ``description`` is a tri-state: when ``set_description`` is
        true the new value (possibly ``None`` to clear) is written; otherwise the
        existing description is left untouched. Returns the updated entity, or
        ``None`` if no row matches in this tenant (the service maps that to 404).
        Ownership is enforced one layer up — this only guarantees tenancy.
        """
        stmt = select(models.Collection).where(
            models.Collection.tenant_id == self._tenant_id,
            models.Collection.id == collection_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if set_description:
            row.description = description
        await self._session.flush()
        await self._session.refresh(row)
        return _to_collection(row)

    async def delete(self, collection_id: UUID) -> bool:
        stmt = select(models.Collection).where(
            models.Collection.tenant_id == self._tenant_id,
            models.Collection.id == collection_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True


class DocumentRepository(_TenantScopedRepository):
    """Documents within one tenant."""

    async def create(
        self,
        *,
        owner_id: UUID,
        collection_id: UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        status: DocumentStatus = DocumentStatus.PENDING,
    ) -> Document:
        row = models.Document(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            collection_id=collection_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status=status.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_document(row)

    async def get(self, document_id: UUID) -> Document | None:
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_document(row) if row is not None else None

    async def list_in_collection(self, collection_id: UUID) -> list[Document]:
        stmt = (
            select(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.collection_id == collection_id,
            )
            .order_by(models.Document.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document(r) for r in rows]

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        collection_id: UUID | None = None,
        status: DocumentStatus | None = None,
        filename_query: str | None = None,
    ) -> list[Document]:
        """A keyset page of an owner's documents (newest first), optionally filtered.

        Owner-scoped *and* tenant-scoped (deny-by-default ownership, spec 0004
        §2.2 + INV-1): a caller only ever sees their own documents. Ordered by
        ``(created_at, id)`` **descending** with ``id`` the stable tiebreaker, so
        the total order is deterministic even when two rows share a timestamp.
        ``after_id`` is the id of the last row of the previous page (the decoded
        cursor); rows strictly *after* it in that order are returned, capped at
        ``limit``. The optional ``collection_id`` / ``status`` / ``filename_query``
        narrow the result (the contract's list filters); ``filename_query`` is a
        case-insensitive substring match on the filename (lexical, not semantic).

        Like the collections keyset, the cursor boundary's ``created_at`` is
        resolved by a **correlated scalar subquery** rather than a client-supplied
        timestamp, so the comparison is exact on Postgres and on the SQLite used by
        the offline tests (no lossy ``datetime`` round-trip across the wire).
        """
        conditions = [
            models.Document.tenant_id == self._tenant_id,
            models.Document.owner_id == owner_id,
        ]
        if collection_id is not None:
            conditions.append(models.Document.collection_id == collection_id)
        if status is not None:
            conditions.append(models.Document.status == status.value)
        if filename_query:
            conditions.append(models.Document.filename.ilike(f"%{filename_query}%"))
        if after_id is not None:
            boundary_created_at = (
                select(models.Document.created_at)
                .where(
                    models.Document.tenant_id == self._tenant_id,
                    models.Document.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Document.created_at < boundary_created_at,
                    and_(
                        models.Document.created_at == boundary_created_at,
                        models.Document.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Document)
            .where(*conditions)
            .order_by(models.Document.created_at.desc(), models.Document.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document(r) for r in rows]

    async def count_chunks(self, document_id: UUID) -> int:
        """Count indexed chunks for a document (the wire ``chunk_count``).

        Tenant-scoped on both the document and its chunks (INV-1). Returns ``0``
        until ingestion (#21) populates chunks; the service has already
        established the document's visibility before asking.
        """
        stmt = (
            select(func.count())
            .select_from(models.Chunk)
            .where(
                models.Chunk.tenant_id == self._tenant_id,
                models.Chunk.document_id == document_id,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def delete(self, document_id: UUID) -> bool:
        """Delete a document (tenant-scoped); cascades to its chunks.

        Returns ``False`` when no row matches in this tenant (the service maps
        that to 404). Ownership is enforced one layer up — this only guarantees
        tenancy. The ORM ``delete-orphan`` cascade removes the document's chunks
        in the same transaction (INV-1: same-tenant chunks only).
        """
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def set_status(
        self, document_id: UUID, status: DocumentStatus, *, error: str | None = None
    ) -> Document | None:
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        row.error = error
        await self._session.flush()
        # The ``onupdate`` server default refreshed ``updated_at`` server-side;
        # reload so the mapper reads the new value without a lazy emit.
        await self._session.refresh(row)
        return _to_document(row)


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """One chunk to persist for a document (ingestion, #21).

    The plain inputs a :class:`ChunkRepository.replace_for_document` write needs:
    text + its source span + the (optional) embedding. ``ord`` is assigned by the
    repository from list position so the ``(document_id, ord)`` uniqueness holds
    and the order is contiguous.
    """

    text: str
    char_start: int
    char_end: int
    embedding: Sequence[float] | None = None


class ChunkRepository(_TenantScopedRepository):
    """Chunks (passages + embeddings) within one tenant (#21 ingestion)."""

    async def add(
        self,
        *,
        document_id: UUID,
        ord: int,
        text: str,
        char_start: int,
        char_end: int,
        embedding: Sequence[float] | None = None,
    ) -> Chunk:
        row = models.Chunk(
            tenant_id=self._tenant_id,
            document_id=document_id,
            ord=ord,
            text=text,
            char_start=char_start,
            char_end=char_end,
            embedding=list(embedding) if embedding is not None else None,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_chunk(row)

    async def delete_for_document(self, document_id: UUID) -> int:
        """Delete every chunk of a document (tenant-scoped). Returns the count.

        Tenant-scoped on the chunk rows (INV-1): a tenant-B repository deletes
        nothing belonging to tenant A. Used by :meth:`replace_for_document` to
        make re-ingestion idempotent (a re-run replaces, never duplicates).
        """
        stmt = select(models.Chunk).where(
            models.Chunk.tenant_id == self._tenant_id,
            models.Chunk.document_id == document_id,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)

    async def replace_for_document(
        self, document_id: UUID, chunks: Sequence[ChunkInput]
    ) -> list[Chunk]:
        """Replace all of a document's chunks with ``chunks`` (idempotent, #21).

        The idempotency primitive for ingestion (AC-5): re-running the task for
        the same document **replaces** its chunk set rather than duplicating it.
        Deletes the existing chunks first (so the ``(document_id, ord)`` unique
        constraint never collides on a re-run), then inserts the new set with
        contiguous ``ord`` assigned from list position. Tenant-scoped throughout
        (INV-1): a foreign-tenant ``document_id`` deletes nothing and the
        inserts carry this repository's tenant. The caller owns the transaction
        boundary, so the delete + inserts commit atomically — a re-run is never
        observed half-applied.
        """
        await self.delete_for_document(document_id)
        rows = [
            models.Chunk(
                tenant_id=self._tenant_id,
                document_id=document_id,
                ord=ordinal,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                embedding=list(chunk.embedding) if chunk.embedding is not None else None,
            )
            for ordinal, chunk in enumerate(chunks)
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return [_to_chunk(row) for row in rows]

    async def get(self, chunk_id: UUID) -> Chunk | None:
        stmt = select(models.Chunk).where(
            models.Chunk.tenant_id == self._tenant_id,
            models.Chunk.id == chunk_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_chunk(row) if row is not None else None

    async def list_for_document(self, document_id: UUID) -> list[Chunk]:
        stmt = (
            select(models.Chunk)
            .where(
                models.Chunk.tenant_id == self._tenant_id,
                models.Chunk.document_id == document_id,
            )
            .order_by(models.Chunk.ord.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_chunk(r) for r in rows]


class ChatSessionRepository(_TenantScopedRepository):
    """Chat sessions within one tenant."""

    async def create(self, *, owner_id: UUID, model: str, title: str = "") -> ChatSessionEntity:
        row = models.ChatSession(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            model=model,
            title=title,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_chat_session(row)

    async def get(self, session_id: UUID) -> ChatSessionEntity | None:
        stmt = select(models.ChatSession).where(
            models.ChatSession.tenant_id == self._tenant_id,
            models.ChatSession.id == session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_chat_session(row) if row is not None else None

    async def list_for_owner(self, owner_id: UUID) -> list[ChatSessionEntity]:
        stmt = (
            select(models.ChatSession)
            .where(
                models.ChatSession.tenant_id == self._tenant_id,
                models.ChatSession.owner_id == owner_id,
            )
            .order_by(models.ChatSession.updated_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_chat_session(r) for r in rows]

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[ChatSessionEntity]:
        """A keyset page of an owner's chat sessions (newest-updated first).

        Owner- *and* tenant-scoped (spec 0004 §2.2 + INV-1): a caller only ever
        sees their own sessions. Ordered by ``(updated_at, id)`` **descending**
        with ``id`` the stable tiebreaker, so the order is deterministic even
        when two rows share a timestamp. ``after_id`` is the decoded cursor (the
        previous page's last id); rows strictly after it are returned, capped at
        ``limit``. The boundary's ``updated_at`` is resolved by a correlated
        scalar subquery (exact on Postgres + the offline SQLite, no timestamp
        crosses the wire), mirroring the collections/documents keyset.
        """
        conditions = [
            models.ChatSession.tenant_id == self._tenant_id,
            models.ChatSession.owner_id == owner_id,
        ]
        if after_id is not None:
            boundary_updated_at = (
                select(models.ChatSession.updated_at)
                .where(
                    models.ChatSession.tenant_id == self._tenant_id,
                    models.ChatSession.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.ChatSession.updated_at < boundary_updated_at,
                    and_(
                        models.ChatSession.updated_at == boundary_updated_at,
                        models.ChatSession.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.ChatSession)
            .where(*conditions)
            .order_by(models.ChatSession.updated_at.desc(), models.ChatSession.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_chat_session(r) for r in rows]

    async def count_messages(self, session_id: UUID) -> int:
        """Count messages in a session (the wire ``message_count``).

        Tenant-scoped on both the session and its messages (INV-1). Returns ``0``
        for an empty or non-existent session — the service establishes visibility
        before asking.
        """
        stmt = (
            select(func.count())
            .select_from(models.Message)
            .where(
                models.Message.tenant_id == self._tenant_id,
                models.Message.session_id == session_id,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def update(
        self,
        session_id: UUID,
        *,
        title: str | None = None,
        model: str | None = None,
    ) -> ChatSessionEntity | None:
        """Apply a partial update (rename / change default model), tenant-scoped.

        Only supplied fields are touched. Returns the updated entity, or ``None``
        if no row matches in this tenant (the service maps that to 404). Ownership
        is enforced one layer up — this only guarantees tenancy.
        """
        stmt = select(models.ChatSession).where(
            models.ChatSession.tenant_id == self._tenant_id,
            models.ChatSession.id == session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if title is not None:
            row.title = title
        if model is not None:
            row.model = model
        await self._session.flush()
        await self._session.refresh(row)
        return _to_chat_session(row)

    async def touch(self, session_id: UUID) -> None:
        """Bump a session's ``updated_at`` so a new turn re-sorts it to the top.

        Tenant-scoped (INV-1). A no-op for a foreign/missing id. Used by the send
        path so an active conversation surfaces first in the session list.
        """
        stmt = select(models.ChatSession).where(
            models.ChatSession.tenant_id == self._tenant_id,
            models.ChatSession.id == session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def delete(self, session_id: UUID) -> bool:
        stmt = select(models.ChatSession).where(
            models.ChatSession.tenant_id == self._tenant_id,
            models.ChatSession.id == session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True


class MessageRepository(_TenantScopedRepository):
    """Messages within one tenant."""

    async def add(
        self,
        *,
        session_id: UUID,
        role: MessageRole,
        content: str,
        model: str | None = None,
    ) -> Message:
        row = models.Message(
            tenant_id=self._tenant_id,
            session_id=session_id,
            role=role.value,
            content=content,
            model=model,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_message(row)

    async def add_with_id(
        self,
        *,
        message_id: UUID,
        session_id: UUID,
        role: MessageRole,
        content: str,
        model: str | None = None,
    ) -> Message:
        """Persist a message under a **pre-minted** id (the streamed answer path).

        The chat runtime mints the assistant message id up front so it can ride
        the WS ``start`` envelope (``messageId``) *before* the row exists, then
        persists the finished answer under that exact id — so the streamed id and
        the stored row always agree, and the citations attach to it. Tenant-scoped
        (INV-1).
        """
        row = models.Message(
            id=message_id,
            tenant_id=self._tenant_id,
            session_id=session_id,
            role=role.value,
            content=content,
            model=model,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_message(row)

    async def get(self, message_id: UUID) -> Message | None:
        stmt = select(models.Message).where(
            models.Message.tenant_id == self._tenant_id,
            models.Message.id == message_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_message(row) if row is not None else None

    async def list_for_session(self, session_id: UUID) -> list[Message]:
        stmt = (
            select(models.Message)
            .where(
                models.Message.tenant_id == self._tenant_id,
                models.Message.session_id == session_id,
            )
            .order_by(models.Message.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_message(r) for r in rows]

    async def list_for_session_page(
        self,
        session_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[Message]:
        """A keyset page of a session's messages (oldest → newest, contract order).

        Tenant-scoped (INV-1). Ordered by ``(created_at, id)`` **ascending** —
        message history reads oldest-first (contracts/openapi.yaml ``listMessages``)
        — with ``id`` the stable tiebreaker. ``after_id`` is the decoded cursor
        (previous page's last id); rows strictly after it are returned, capped at
        ``limit``. The boundary's ``created_at`` is resolved by a correlated scalar
        subquery so the comparison is exact on Postgres + the offline SQLite.
        """
        conditions = [
            models.Message.tenant_id == self._tenant_id,
            models.Message.session_id == session_id,
        ]
        if after_id is not None:
            boundary_created_at = (
                select(models.Message.created_at)
                .where(
                    models.Message.tenant_id == self._tenant_id,
                    models.Message.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Message.created_at > boundary_created_at,
                    and_(
                        models.Message.created_at == boundary_created_at,
                        models.Message.id > after_id,
                    ),
                )
            )
        stmt = (
            select(models.Message)
            .where(*conditions)
            .order_by(models.Message.created_at.asc(), models.Message.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_message(r) for r in rows]


@dataclass(frozen=True, slots=True)
class CitationView:
    """A citation joined to its source chunk + document (the wire ``Citation``).

    The stored :class:`~app.domain.entities.Citation` carries only ``chunk_id`` +
    the span + score; the contract's ``Citation`` also needs the source
    ``document_id`` / ``document_name`` and the ``snippet`` text. This view is the
    citation row joined (tenant-scoped) to its chunk and that chunk's document, so
    ``GET .../messages`` can render a deep-linkable reference (CC-11 AC-2/AC-3)
    without a per-row N+1. A citation only exists for a permitted passage (INV-3),
    so the join never reveals foreign content.
    """

    id: UUID
    message_id: UUID
    document_id: UUID
    document_name: str
    chunk_id: UUID
    snippet: str
    char_start: int
    char_end: int
    score: float | None


class CitationRepository(_TenantScopedRepository):
    """Citations within one tenant (INV-3)."""

    async def add(
        self,
        *,
        message_id: UUID,
        chunk_id: UUID,
        char_start: int,
        char_end: int,
        score: float | None = None,
    ) -> Citation:
        row = models.Citation(
            tenant_id=self._tenant_id,
            message_id=message_id,
            chunk_id=chunk_id,
            char_start=char_start,
            char_end=char_end,
            score=score,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_citation(row)

    async def list_for_message(self, message_id: UUID) -> list[Citation]:
        stmt = (
            select(models.Citation)
            .where(
                models.Citation.tenant_id == self._tenant_id,
                models.Citation.message_id == message_id,
            )
            .order_by(models.Citation.char_start.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_citation(r) for r in rows]

    async def list_for_message_hydrated(self, message_id: UUID) -> list[CitationView]:
        """Citations for a message, joined to source document + chunk text.

        Tenant-scoped on the citation, its chunk, and that chunk's document
        (INV-1, defense in depth). Returns the contract-shaped :class:`CitationView`
        (document id/name + snippet) ordered by char span, so the messages route
        renders a deep-linkable reference without an N+1.
        """
        stmt = (
            select(
                models.Citation.id,
                models.Citation.message_id,
                models.Citation.chunk_id,
                models.Citation.char_start,
                models.Citation.char_end,
                models.Citation.score,
                models.Chunk.text,
                models.Document.id,
                models.Document.filename,
            )
            .join(models.Chunk, models.Chunk.id == models.Citation.chunk_id)
            .join(models.Document, models.Document.id == models.Chunk.document_id)
            .where(
                models.Citation.tenant_id == self._tenant_id,
                models.Citation.message_id == message_id,
                models.Chunk.tenant_id == self._tenant_id,
                models.Document.tenant_id == self._tenant_id,
            )
            .order_by(models.Citation.char_start.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            CitationView(
                id=row[0],
                message_id=row[1],
                chunk_id=row[2],
                char_start=row[3],
                char_end=row[4],
                score=row[5],
                snippet=row[6],
                document_id=row[7],
                document_name=row[8],
            )
            for row in rows
        ]

    async def list_for_messages_hydrated(
        self, message_ids: Sequence[UUID]
    ) -> dict[UUID, list[CitationView]]:
        """Batch the hydrated-citation join across many messages (history page).

        One query for a page of messages (avoids N+1 on ``listMessages``), grouped
        by ``message_id``. Tenant-scoped throughout (INV-1). Messages with no
        citations are simply absent from the map (the caller defaults to ``[]``).
        """
        if not message_ids:
            return {}
        stmt = (
            select(
                models.Citation.id,
                models.Citation.message_id,
                models.Citation.chunk_id,
                models.Citation.char_start,
                models.Citation.char_end,
                models.Citation.score,
                models.Chunk.text,
                models.Document.id,
                models.Document.filename,
            )
            .join(models.Chunk, models.Chunk.id == models.Citation.chunk_id)
            .join(models.Document, models.Document.id == models.Chunk.document_id)
            .where(
                models.Citation.tenant_id == self._tenant_id,
                models.Citation.message_id.in_(list(message_ids)),
                models.Chunk.tenant_id == self._tenant_id,
                models.Document.tenant_id == self._tenant_id,
            )
            .order_by(models.Citation.char_start.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        grouped: dict[UUID, list[CitationView]] = {}
        for row in rows:
            grouped.setdefault(row[1], []).append(
                CitationView(
                    id=row[0],
                    message_id=row[1],
                    chunk_id=row[2],
                    char_start=row[3],
                    char_end=row[4],
                    score=row[5],
                    snippet=row[6],
                    document_id=row[7],
                    document_name=row[8],
                )
            )
        return grouped


class AuditEventRepository(_TenantScopedRepository):
    """Append-only product-audit log within one tenant (spec 0004 §2.4).

    Exposes only ``record`` (append) and reads — there is intentionally no
    update or delete method; the table also denies UPDATE/DELETE at the DB role
    (the migration). Reading the audit trail is a ``security``-role action,
    gated in ``services/`` (INV-5), not here.
    """

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        actor_id: UUID | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuditEvent:
        row = models.AuditEvent(
            tenant_id=self._tenant_id,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome.value,
            request_id=request_id,
            source_ip=source_ip,
            event_metadata=metadata or {},
        )
        self._session.add(row)
        await self._session.flush()
        return _to_audit_event(row)

    async def list_recent(self, *, limit: int = 100) -> list[AuditEvent]:
        stmt = (
            select(models.AuditEvent)
            .where(models.AuditEvent.tenant_id == self._tenant_id)
            .order_by(models.AuditEvent.ts.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_audit_event(r) for r in rows]
