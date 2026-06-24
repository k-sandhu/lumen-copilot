"""SQLAlchemy 2.0 ORM models — the MVP relational schema (spec 0004 §4).

These are the **only** persistence-shape definitions; nobody outside ``db/`` may
import them (ADR-0004 boundary). They mirror, but are distinct from, the pure
domain entities in ``app.domain.entities`` — repositories map row ⇄ entity so a
``Session`` and these ORM rows never leak upward (backend/AGENTS.md).

Tenancy & ownership invariants baked into the schema (spec 0004 §2.1/§2.2):

* **Every tenant-scoped table** carries a non-null ``tenant_id`` FK → ``tenants``,
  indexed, so the repository tenant predicate (INV-1) is always cheap and the
  RLS backstop (a follow-up, §2.1) has a column to key off.
* **Ownership-bearing tables** (``collections``, ``documents``, ``chat_sessions``,
  ``sources``) carry ``owner_id`` FK → ``users`` — the "user sees only their own"
  default.
* ``chunks`` keeps the embedding **in-row** beside ``tenant_id`` + the document
  FK so permission-aware retrieval is one ``WHERE`` clause (spec 0004 §4).
* ``audit_events`` is append-only (the app DB role gets no UPDATE/DELETE on it —
  enforced in the migration, §2.4); the model is write-then-read only.

The pgvector column width comes from settings (``LLM_EMBEDDING_DIMENSIONS``); the
migration pins the same literal so the table and the model agree.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.core.config import get_settings
from app.db.base import Base
from app.db.types import Embedding, StringArray

# JSON-typed metadata: JSONB on Postgres (indexable), generic JSON elsewhere
# (SQLite test runs). JSONB() is untyped upstream; the ignore is local + narrow.
_JSON = JSON().with_variant(JSONB(), "postgresql")  # type: ignore[no-untyped-call]


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns, server-defaulted in UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantScopedMixin:
    """A non-null, indexed ``tenant_id`` FK on every tenant-scoped table.

    The single most important column in the schema: it is the predicate the
    repositories require (INV-1) and the key the RLS backstop will use. Indexed
    because *every* scoped query filters on it.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


def _pk() -> Mapped[uuid.UUID]:
    """A UUID primary key, generated app-side (portable across dialects)."""
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Tenant(TimestampMixin, Base):
    """A customer boundary — the root every ``tenant_id`` references."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class User(TenantScopedMixin, TimestampMixin, Base):
    """An app-managed principal (email + Argon2id hash), one per tenant."""

    __tablename__ = "users"
    __table_args__ = (
        # Email is unique within a tenant, not globally (multi-tenant).
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # RBAC roles (spec 0004 §2.3); a string array — empty = no privileges.
    roles: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)


class RefreshToken(TenantScopedMixin, Base):
    """A rotating, revocable refresh token (spec 0004 §2.3).

    No ``TimestampMixin``/``updated_at``: a token row is created, optionally
    revoked, then expires — it is never re-described. Only the **hash** of the
    opaque token is stored (``token_hash``, unique) so a DB read yields no usable
    token. ``revoked_at`` set ⇒ the token can no longer be used (logout or
    rotation); ``expires_at`` is the hard lifetime cap.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Collection(TenantScopedMixin, TimestampMixin, Base):
    """A folder grouping a user's documents. Ownership-bearing."""

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    documents: Mapped[list[Document]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )


class Source(TenantScopedMixin, TimestampMixin, Base):
    """A connected external source ingested by a connector (ADR-0009 §4).

    Tenant- and owner-scoped, deny-by-default: only the adding user (within
    their tenant) can retrieve what a source ingests (INV-1/INV-2). ``type`` is
    the connector name (e.g. ``web``); ``config`` is the connector's opaque,
    portable JSON (e.g. ``{"url": ..., "mode": ...}``). ``status`` tracks the
    sync lifecycle (``pending|syncing|ready|error``); ``indexed_count`` is how
    many documents the last sync produced, ``last_error`` the failure detail.
    Ingested ``documents`` link back via ``source_id`` and CASCADE on delete, so
    removing a source removes its docs (ADR-0009 §5).
    """

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    documents: Mapped[list[Document]] = relationship(back_populates="source")


class Document(TenantScopedMixin, TimestampMixin, Base):
    """An uploaded file; ingested into ``chunks`` async (#21). Ownership-bearing."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_collection_id", "collection_id"),
        Index("ix_documents_source_id", "source_id"),
        CheckConstraint("size_bytes >= 0", name="ck_documents_size_nonneg"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The source this doc was ingested from; null for direct uploads (ADR-0009
    # §4). CASCADE so deleting a source removes the docs it produced (§5).
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The object-store key (tenant-prefixed, content-addressed; app.storage.keys).
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    collection: Mapped[Collection] = relationship(back_populates="documents")
    source: Mapped[Source | None] = relationship(back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(TenantScopedMixin, TimestampMixin, Base):
    """A retrievable passage of a document, embedding stored in-row (spec 0004 §4).

    The ``embedding`` column sits beside ``tenant_id`` + ``document_id`` so a
    permission-aware retrieval query is one ``WHERE`` clause. Width =
    ``LLM_EMBEDDING_DIMENSIONS`` (1024). Nullable so a row can exist before the
    embedding is computed (two-phase ingestion, #21).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ord", name="uq_chunks_document_ord"),
        CheckConstraint("char_start >= 0", name="ck_chunks_char_start_nonneg"),
        CheckConstraint("char_end >= char_start", name="ck_chunks_char_span"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Embedding(get_settings().llm_embedding_dimensions), nullable=True
    )
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped[Document] = relationship(back_populates="chunks")


class ChatSession(TenantScopedMixin, TimestampMixin, Base):
    """A conversation thread. Ownership-bearing."""

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(255), nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(TenantScopedMixin, TimestampMixin, Base):
    """One turn in a chat session (oldest → newest by ``created_at``)."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_id", "session_id"),)

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # The model that produced an assistant message; null for user/system turns.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)

    session: Mapped[ChatSession] = relationship(back_populates="messages")
    citations: Mapped[list[Citation]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class Citation(TenantScopedMixin, TimestampMixin, Base):
    """A passage-level reference from an assistant message (INV-3).

    Resolves to a ``chunk`` the caller was permitted to retrieve; the span is
    message-relative (contracts/openapi.yaml Citation char_start/char_end).
    """

    __tablename__ = "citations"
    __table_args__ = (
        CheckConstraint("char_start >= 0", name="ck_citations_char_start_nonneg"),
        CheckConstraint("char_end >= char_start", name="ck_citations_char_span"),
    )

    id: Mapped[uuid.UUID] = _pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float | None] = mapped_column(nullable=True)

    message: Mapped[Message] = relationship(back_populates="citations")


class AuditEvent(TenantScopedMixin, Base):
    """One append-only product-audit record (spec 0004 §2.4).

    No ``TimestampMixin`` / ``updated_at``: an audit event is immutable. ``ts``
    is the event time (server-defaulted). The application DB role is granted no
    UPDATE/DELETE on this table (the migration revokes them) so the log is
    tamper-evident by construction; the per-event hash-chain is a follow-up
    (§2.4 ``revisit-at-implementation``).
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_ts", "tenant_id", "ts"),
        Index("ix_audit_events_actor_id", "actor_id"),
        # Composite indexes for the filtered, time-ordered GET /audit reads (#82):
        # each leads with tenant_id (INV-1) and ends with ts so one index serves
        # both the equality filter and the `ORDER BY ts DESC` the read uses.
        Index("ix_audit_events_tenant_action_ts", "tenant_id", "action", "ts"),
        Index("ix_audit_events_tenant_actor_ts", "tenant_id", "actor_id", "ts"),
        Index("ix_audit_events_tenant_resource_ts", "tenant_id", "resource_id", "ts"),
    )

    id: Mapped[uuid.UUID] = _pk()
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Nullable: a `system`/`anonymous` actor has no user id (spec 0004 §2.4).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(
        String(45).with_variant(INET(), "postgresql"), nullable=True
    )
    event_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", _JSON, nullable=False, default=dict
    )


class Grant(TenantScopedMixin, Base):
    """An explicit access grant — the sharing seam behind INV-2 (spec 0004 §2.2).

    No ``TimestampMixin``/``updated_at``: a grant is created then revoked
    (deleted), never re-described — so only ``created_at`` is kept. Each row says
    a ``principal`` (MVP: a ``user``, by ``principal_id``) may access a
    ``resource`` (a ``collection`` or ``document``, by ``resource_id``) within
    this tenant, at ``role`` (MVP: ``viewer``). ``resource_id``/``principal_id``
    are plain UUID columns rather than FKs because the referent's *table* varies
    with the type (a document vs a collection; a user vs — later — a group): the
    grant service validates existence + ownership before inserting.

    The ``UNIQUE(tenant_id, resource_type, resource_id, principal_type,
    principal_id)`` makes re-granting idempotent; the two composite indexes serve
    the retrieval filter (by principal) and the grant service (by resource). The
    ``CheckConstraint``s pin the enum domains at the DB so a bad type can never be
    stored. Tenant-scoped like every table (INV-1); the ``0008`` migration also
    puts it under the RLS backstop.
    """

    __tablename__ = "grants"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            name="uq_grants_resource_principal",
        ),
        Index("ix_grants_tenant_principal", "tenant_id", "principal_id"),
        Index("ix_grants_tenant_resource", "tenant_id", "resource_type", "resource_id"),
        CheckConstraint(
            "resource_type in ('collection', 'document')",
            name="ck_grants_resource_type",
        ),
        CheckConstraint(
            "principal_type in ('user', 'group', 'role')",
            name="ck_grants_principal_type",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
