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
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    __table_args__ = (
        # Per-tenant chat tool-use-turn budget override (issue #148): NULL ⇒ use
        # the system default; when set it must sit in the supported band so a
        # misconfiguration can neither disable bounding nor explode answer cost.
        CheckConstraint(
            "max_tool_turns IS NULL OR (max_tool_turns >= 1 AND max_tool_turns <= 50)",
            name="ck_tenants_max_tool_turns_range",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Per-tenant override of the agent's tool-use-turn budget; NULL ⇒ the system
    # default (``Settings.chat_max_tool_turns``). Admin-configurable (issue #148).
    max_tool_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
    Ingested ``documents`` link back via ``source_id``; removing a source removes
    its docs and — when it then holds nothing else — the auto-created backing
    collection (ADR-0009 §5). That cleanup is driven by
    :meth:`~app.services.sources_service.SourcesService.delete`, **not** the
    ``documents.source_id`` FK ``ON DELETE CASCADE``: an ORM parent delete nulls
    the nullable child FK before the DB cascade fires (the #139 orphan bug), so
    the service deletes the documents explicitly. The FK cascade stays as a
    DB-level backstop for non-ORM deletes.
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
    # §4). ON DELETE CASCADE is a DB-level backstop only: the app path deletes a
    # source's docs explicitly in the sources service (an ORM parent delete nulls
    # this nullable FK before the cascade fires — the #139 orphan bug), §5.
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


class UserPreference(TenantScopedMixin, TimestampMixin, Base):
    """A user's account preferences — one row per user (spec 0005, epic #144).

    Created lazily on the first ``PATCH /preferences``: a fresh user has no row,
    and ``GET /preferences`` returns the implicit server-default state without
    writing (read-before-write). ``default_model`` is an override stored as a
    plain string id from the ``/models`` registry — validated at write time and
    fail-closed at chat time (a model later removed from the registry falls back
    to the server default). Tenant-scoped (INV-1); the unique ``(tenant_id,
    user_id)`` makes the row a per-user singleton (the upsert target).
    """

    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_preferences_tenant_user"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SavedSearch(TenantScopedMixin, TimestampMixin, Base):
    """A saved ``/search`` query + its filters (spec 0005, epic #144).

    Ownership-bearing: ``owner_id`` scopes it to the saving user (deny-by-default,
    spec 0004 §2.2). ``query`` + the nullable ``collection_id`` / ``source`` /
    ``type`` are exactly the ``/search`` parameters, so applying a saved search
    re-runs the same query. Tenant-scoped (INV-1) + RLS-backstopped (the 0010
    migration).
    """

    __tablename__ = "saved_searches"
    __table_args__ = (Index("ix_saved_searches_tenant_owner", "tenant_id", "owner_id"),)

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    collection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RecentSearch(TenantScopedMixin, Base):
    """A recent ``/search`` query, per user (spec 0005, epic #144).

    No ``TimestampMixin``: a recent search is created then *touched* (its
    ``last_used_at`` bumped when the same normalized query is run again) — there is
    no separate created/updated pair. De-duplicated by ``(tenant_id, user_id,
    normalized_query)`` so re-running a query updates one row rather than piling up;
    the repository caps the count per user (oldest evicted). Tenant-scoped (INV-1)
    + RLS-backstopped (the 0011 migration). ``query`` keeps the latest display form;
    ``normalized_query`` (trimmed/lower-cased) is the dedupe key.
    """

    __tablename__ = "recent_searches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "normalized_query", name="uq_recent_searches_tenant_user_norm"
        ),
        Index("ix_recent_searches_tenant_user_used", "tenant_id", "user_id", "last_used_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_query: Mapped[str] = mapped_column(String(1000), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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


class Artifact(TenantScopedMixin, Base):
    """A file an agent/run produced — tenant/owner-scoped, immutable (issue #208).

    Distinct from the user-uploaded ``documents`` table: this is where the
    file-writing tool and the code sandbox persist their output (CC-12). Mirrors
    the ``documents`` tenant/owner + storage-key pattern, but is **immutable** —
    no ``updated_at`` (a new version is a new row), and no ``status`` (the bytes
    exist the moment the row does). ``produced_by`` (chat_session|run|tool) is the
    write origin; the three nullable link ids let the artifact point back at the
    producing session/run/tool invocation.

    Only ``session_id`` is a real FK (``chat_sessions`` exists and is tenant-scoped
    + CASCADE like the other links). ``run_id`` and ``tool_invocation_id`` are
    plain nullable UUID columns rather than FKs because their referent tables (the
    run/agent framework, CC-A) do not exist yet — mirroring how ``grants`` keeps
    ``resource_id``/``principal_id`` FK-less because the referent varies; the
    service validates linkage. They become FKs when those tables land.

    Tenant-scoped (INV-1) with the same ``0007``-style RLS backstop applied in the
    ``0013`` migration. ``retention_expires_at`` (nullable ⇒ keep) is the janitor
    purge boundary; ``ix_artifacts_retention`` serves the sweep.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_tenant_owner", "tenant_id", "owner_id"),
        Index("ix_artifacts_session_id", "session_id"),
        # The retention janitor's sweep predicate: rows whose retention has
        # elapsed. Partial index (retention_expires_at IS NOT NULL) on Postgres so
        # the common "keep forever" rows (NULL) do not bloat it; a plain index
        # elsewhere (SQLite test runs ignore the WHERE clause harmlessly).
        Index(
            "ix_artifacts_retention",
            "retention_expires_at",
            postgresql_where=text("retention_expires_at IS NOT NULL"),
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonneg"),
        CheckConstraint(
            "produced_by in ('chat_session', 'run', 'tool')",
            name="ck_artifacts_produced_by",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    produced_by: Mapped[str] = mapped_column(String(20), nullable=False)
    # Optional back-links to the producing context. Only session_id is an FK (its
    # table exists); run_id / tool_invocation_id are plain UUIDs until CC-A lands.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tool_invocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The object-store key (tenant-prefixed, content-addressed; app.storage.keys),
    # under the ``artifacts/`` prefix.
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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


class ToolInvocation(TenantScopedMixin, Base):
    """One governed tool-call record (CC-7 / issue #207 §4) — the tool trace/audit row.

    No ``TimestampMixin``/``updated_at``: an invocation record is written once and
    never re-described — so only ``created_at`` is kept (like ``audit_events`` and
    ``grants``). One row **per invocation**, whatever the outcome: a success, an
    off-allow-list/unapproved **denial**, or a tool **failure** are all recorded
    (``ok`` + ``error``), so the trace has no silent gap. This feeds the WS trace
    now and AgentOps analytics later (E14) — it is not itself the append-only audit
    log (that stays ``audit_events``), so it is an ordinary tenant-scoped table
    (the app role may write it; it is not UPDATE/DELETE-revoked).

    ``args_hash`` is a stable, non-reversible hash of the call args (never the raw
    args — the same discipline as the audit query hash, spec 0004 §2.4). ``run_id``
    is reserved for a future multi-step agent-run grouping (nullable, MVP unused).
    Tenant-leading indexes serve the two reads: by session (the trace for a chat)
    and by (tenant, tool) (per-tool analytics). RLS: put under the same fail-closed
    ``app.tenant_id`` backstop as every tenant-scoped table (the migration).
    """

    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tool_invocations_tenant_session", "tenant_id", "session_id"),
        Index("ix_tool_invocations_tenant_tool", "tenant_id", "tool_name"),
        CheckConstraint("duration_ms >= 0", name="ck_tool_invocations_duration_nonneg"),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The chat session the call ran in (nullable: an ad-hoc/non-session invocation
    # path may not carry one). SET NULL so deleting a session keeps the trace rows.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The assistant message the call contributed to (nullable, same rationale).
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Reserved for a future multi-step agent-run grouping (E14); no FK yet.
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # The ``ERROR_*`` code when ``ok`` is False (governance denial or failure);
    # null on success.
    error: Mapped[str | None] = mapped_column(String(50), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
