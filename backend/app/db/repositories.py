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
from uuid import UUID

from sqlalchemy import select
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
