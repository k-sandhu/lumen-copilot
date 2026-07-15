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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
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
    # Per-tenant application logo (object-store key); NULL ⇒ the default brand mark.
    logo_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)


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
    # Per-user profile avatar (object-store key); NULL ⇒ the initials fallback in the
    # shell. Keyed ``{tenant_id}/{user_id}/{sha}/{filename}`` — the tenant prefix is
    # the isolation seam, the user_id segment scopes it to the owning user. The user
    # manages their OWN avatar (not admin-gated); the shell renders it via a presigned
    # GET URL delivered on ``GET /auth/me``.
    avatar_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)


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
    __table_args__ = (Index("ix_chat_sessions_assistant_id", "assistant_id"),)

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional assistant pin (ADR-0011 §1): a session started from an assistant
    # records both which assistant and the exact pinned version it ran, so the
    # transcript is reproducible after the assistant is edited/rolled back. Both
    # NULL for ad-hoc chat (the default, unchanged path). SET NULL on delete so a
    # deleted assistant/version does not cascade-delete the conversation.
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistants.id", ondelete="SET NULL"),
        nullable=True,
    )
    assistant_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistant_versions.id", ondelete="SET NULL"),
        nullable=True,
    )

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
    # A per-user free-text instruction prepended to the chat system prompt (the
    # user's persona/preamble). NULL ⇒ no custom instructions. Capped in the wire
    # model (422 over-limit); composed BEFORE the grounding contract at chat time so
    # it can shape tone/scope but never remove grounding/citation rules (INV-3).
    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class Secret(TenantScopedMixin, TimestampMixin, Base):
    """An encrypted per-tenant credential — the secrets vault row (issue #209).

    The credential seam for MCP registration (E3) and a hosted web-search key
    (SPIKE-4). Tenant- and owner-scoped, deny-by-default (INV-1/§2.2): only the
    owner (or a tenant admin) may see or use it, and a cross-tenant id is
    invisible. **No column ever holds plaintext** — the credential lives only as
    ``ciphertext`` + ``nonce`` under ``key_version`` (AES-256-GCM envelope
    encryption, ``app.core.crypto``); ``hint`` is a non-reversing masked tail (e.g.
    the last four chars) so the UI can show *which* secret without the value.

    ``ciphertext``/``nonce`` are ``LargeBinary`` → ``bytea`` on Postgres, ``BLOB``
    on the SQLite test engine (portable, like ``db/types``). ``kind`` is a
    ``CheckConstraint``-pinned enum domain so a bad kind can never be stored. The
    ``UNIQUE(tenant_id, owner_id, name)`` makes a secret name a per-owner singleton
    (a stable handle an adapter can look up + the re-store/rotate target).
    Tenant-scoped like every table (INV-1); the ``0013`` migration also puts it
    under the RLS backstop and denies the app role nothing extra — reads/writes go
    through the write-only service, never a router.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "name", name="uq_secrets_owner_name"),
        Index("ix_secrets_tenant_owner", "tenant_id", "owner_id"),
        CheckConstraint(
            "kind in ('mcp_auth', 'search_api', 'other')",
            name="ck_secrets_kind",
        ),
        CheckConstraint("key_version >= 1", name="ck_secrets_key_version_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # The envelope: encrypted bytes (with GCM's auth tag) + the per-encryption
    # nonce + the key version that produced it. Never the plaintext (issue #209).
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # A non-reversing masked tail for the UI (e.g. last 4 chars). Not the value.
    hint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Who created it (owner or a tenant admin); SET NULL so removing that user does
    # not cascade-delete the secret.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class McpServer(TenantScopedMixin, TimestampMixin, Base):
    """A registered remote MCP server (ADR-0012 §5, issue #226). Ownership-bearing.

    Tenant- and owner-scoped, deny-by-default (INV-1/§2.2): only the registering
    user (or a tenant admin) sees/manages it, and a cross-tenant id is invisible.
    **No column holds the credential** — the auth token/header lives in the CC-C
    ``secrets`` vault (issue #209), and this row keeps only ``auth_secret_ref`` (the
    secret's id, SET NULL so removing the secret does not orphan the server) plus
    ``secret_hint`` (a masked tail projected from the secret at register/rotate for
    the UI). ``transport`` / ``status`` are ``CheckConstraint``-pinned enum domains
    (remote transports only — ``stdio`` can never be stored; ADR-0012 §1).
    ``endpoint_url`` is the https endpoint (SSRF-checked in the service before a row
    is written, and again on every connect). ``discovered_tools`` is the last
    ``list_tools()`` snapshot as portable JSON. Tenant-scoped like every table
    (INV-1); the ``0020`` migration puts it under the RLS backstop.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (
        Index("ix_mcp_servers_tenant_owner", "tenant_id", "owner_id"),
        CheckConstraint(
            "transport in ('streamable_http', 'sse')",
            name="ck_mcp_servers_transport",
        ),
        CheckConstraint(
            "status in ('pending', 'ready', 'error')",
            name="ck_mcp_servers_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    # The CC-C secret's id (a SecretRef.id), never the credential itself; SET NULL
    # so deleting the vault secret does not cascade-delete the server row.
    auth_secret_ref: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("secrets.id", ondelete="SET NULL"),
        nullable=True,
    )
    # A masked tail of the stored credential for the UI (e.g. ``****abcd``); never
    # the value. NULL when no auth is stored (an anonymous server).
    secret_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    last_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The last list_tools() snapshot (names + schemas + read-only annotations).
    discovered_tools: Mapped[list[dict[str, object]]] = mapped_column(
        _JSON, nullable=False, default=list
    )


class LlmProvider(TenantScopedMixin, TimestampMixin, Base):
    """A registered per-tenant LLM provider (foundation PR). Ownership-bearing.

    The persistence half of per-tenant LLM provider registration: a tenant admin
    registers an OpenAI-compatible provider (name + base URL + API key), the backend
    discovers its models via ``GET {base_url}/models`` and stores a snapshot + health
    status. Tenant- and owner-scoped, deny-by-default (INV-1/§2.2): only the
    registering admin (or another tenant admin) sees/manages it, and a cross-tenant
    id is invisible. Mirrors :class:`McpServer` throughout (per-tenant registration +
    a CC-C secret ref + auto-discovery + health status).

    **No column holds the API key** — it lives in the CC-C ``secrets`` vault (#209),
    and this row keeps only ``api_key_secret_ref`` (the secret's id, SET NULL so
    removing the secret does not orphan the provider) plus ``secret_hint`` (a masked
    tail projected from the secret at register/rotate for the UI). ``provider_type`` /
    ``status`` are ``CheckConstraint``-pinned enum domains (``openai_compatible`` only
    for now; the enum is small but extensible). ``base_url`` is the OpenAI-compatible
    API root (SSRF-checked in the service before a row is written, and again on every
    discovery — the same ``resolve_safe_ip`` guard MCP uses). ``discovered_models`` is
    the last ``GET /models`` snapshot as portable JSON (a list of
    ``{"id": str, "label": str | null}``). Tenant-scoped like every table (INV-1);
    the ``0023`` migration puts it under the RLS backstop.

    This is the model-catalog foundation only: routing chat/embeddings through a
    registered provider (and surfacing it in the chat model picker) is a follow-up PR.
    """

    __tablename__ = "llm_providers"
    __table_args__ = (
        Index("ix_llm_providers_tenant_owner", "tenant_id", "owner_id"),
        Index("ix_llm_providers_tenant_enabled", "tenant_id", "enabled"),
        CheckConstraint(
            "provider_type in ('openai_compatible')",
            name="ck_llm_providers_provider_type",
        ),
        CheckConstraint(
            "status in ('pending', 'ready', 'error')",
            name="ck_llm_providers_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The admin who registered it (INV-2). CASCADE — a deprovisioned admin's
    # providers go with them (live config, not a completed record).
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # → a CC-C secret (#209); NEVER the API key itself. SET NULL so deleting the
    # vault secret does not cascade-delete the provider row.
    api_key_secret_ref: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("secrets.id", ondelete="SET NULL"),
        nullable=True,
    )
    # A masked tail of the stored API key for the UI (e.g. ``****abcd``); never the
    # value. NULL when no key is stored (an anonymous provider).
    secret_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    last_discovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The last GET /models snapshot: a list of {"id": str, "label": str | null}.
    discovered_models: Mapped[list[dict[str, object]]] = mapped_column(
        _JSON, nullable=False, default=list
    )


class TenantToolPolicy(TenantScopedMixin, TimestampMixin, Base):
    """A per-tenant admin override of one tool's governance (issue #223).

    The persistence half of admin tool governance: one row is an admin's decision
    for one registered tool within one tenant — is it ``enabled`` for the tenant,
    and does an invocation still ``require_approval``. **Absence of a row means the
    tool's built-in default applies** (deny-by-default): a ``requires_approval``
    tool (e.g. ``run_python``) stays denied until an admin writes a row that
    enables it AND pre-approves it (``enabled=true`` + ``requires_approval=false``).
    That is the switch the policy-driven approval gate consults at invoke time.

    ``tool_name`` is the registry's single source of truth (validated against the
    registry in the service before a write — no free-text tool can be stored). The
    unique ``(tenant_id, tool_name)`` constraint makes the override a per-tenant
    upsert; ``updated_by`` records the admin who last set it (audit corroboration).
    Tenant-scoped like every table (INV-1); the ``0021`` migration puts it under
    the same fail-closed RLS backstop as every other tenant-scoped table.
    """

    __tablename__ = "tenant_tool_policy"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "tool_name", name="uq_tenant_tool_policy_tenant_tool"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The registered tool name (validated against the registry in the service).
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The admin who last set this override (INV-6 corroboration); SET NULL so a
    # deprovisioned admin does not cascade-delete the tenant's live policy.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class TenantSandboxPolicy(TenantScopedMixin, TimestampMixin, Base):
    """A per-tenant admin policy for the code-execution sandbox (issue #233).

    The persistence half of admin sandbox governance: one row is an admin's policy for
    the code-execution sandbox (#230/#231) within one tenant — whether code execution
    is ``enabled`` for the tenant, the package allow/deny lists, the outbound egress
    posture (``egress_allowed`` + ``egress_allowlist``), and the runtime / memory /
    quota caps. **Absence of a row means code execution stays DISABLED for the tenant**
    (deny-by-default / fail-closed, matching #230's deploy-wide kill-switch default
    OFF).

    The stored caps are CEILINGS the enforcement path clamps to the deploy-wide
    ``SANDBOX_*`` config — a per-tenant policy can only NARROW, never widen (a runtime
    above the config cap is clamped; the metadata IP is never egress-allowlistable).
    The unique ``(tenant_id)`` constraint makes the policy a per-tenant singleton;
    ``updated_by`` records the admin who last set it (audit corroboration). Tenant-
    scoped like every table (INV-1); the ``0022`` migration puts it under the same
    fail-closed RLS backstop as every other tenant-scoped table.
    """

    __tablename__ = "tenant_sandbox_policy"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_sandbox_policy_tenant"),
        CheckConstraint("max_runtime_s > 0", name="ck_tenant_sandbox_policy_runtime_pos"),
        CheckConstraint("max_memory_mb > 0", name="ck_tenant_sandbox_policy_memory_pos"),
        CheckConstraint("daily_runtime_cap_s > 0", name="ck_tenant_sandbox_policy_daily_pos"),
        CheckConstraint(
            "max_concurrency > 0", name="ck_tenant_sandbox_policy_concurrency_pos"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # Deny-by-default: a stored row does NOT imply enabled — an admin must turn it on.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Package governance: an allowlist (empty ⇒ curated base image only) + a denylist
    # (takes precedence over the allowlist). JSON string arrays.
    allowed_packages: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    denied_packages: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    # Egress governance: deny-by-default network posture + the host:port allowlist (the
    # metadata IP is stripped in the service — never reachable, G4).
    egress_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    egress_allowlist: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    # Runtime / memory / quota caps — CEILINGS clamped to the deploy-wide SANDBOX_*
    # config in the enforcement path (a per-tenant value only ever narrows).
    max_runtime_s: Mapped[int] = mapped_column(Integer, nullable=False)
    max_memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_runtime_cap_s: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    # The admin who last set this policy (INV-6 corroboration); SET NULL so a
    # deprovisioned admin does not cascade-delete the tenant's live policy.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class TenantAutonomyPolicy(TenantScopedMixin, TimestampMixin, Base):
    """A per-tenant admin autonomy cap (issue #218).

    The persistence half of admin autonomy governance (ADR-0011 §3): one row is an
    admin's ceiling on how far ANY assistant in the tenant may effectively act —
    ``max_autonomy`` is the maximum :class:`~app.domain.entities.AutonomyLevel` an
    assistant's EFFECTIVE autonomy is ``min``'d to. **Absence of a row means no
    ceiling** — an assistant runs at its own configured level (the permissive
    default, mirroring how ``tenant_tool_policy`` / ``tenant_sandbox_policy`` fall
    back to their built-in defaults). Enforcement is two-sided: publishing/upgrading
    an assistant above the cap is rejected/clamped, and the run-time tool gate uses
    the clamped level to decide whether a T1 side-effecting tool may execute.

    ``max_autonomy`` is one of the four enum string values (validated in the service
    and constrained at the DB via a CHECK). The unique ``(tenant_id)`` constraint
    makes the cap a per-tenant singleton; ``updated_by`` records the admin who last
    set it (audit corroboration). Tenant-scoped like every table (INV-1); the
    ``0023`` migration puts it under the same fail-closed RLS backstop as every other
    tenant-scoped table.
    """

    __tablename__ = "tenant_autonomy_policy"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_autonomy_policy_tenant"),
        CheckConstraint(
            "max_autonomy in ('suggest', 'draft', 'act_with_approval', 'act_auto')",
            name="ck_tenant_autonomy_policy_max_autonomy",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The autonomy ceiling — one of the AutonomyLevel enum values (validated in the
    # service; CHECK-constrained above). An assistant's effective autonomy is min'd
    # to this. Absence of a row means no ceiling (the permissive default).
    max_autonomy: Mapped[str] = mapped_column(String(20), nullable=False)
    # The admin who last set this cap (INV-6 corroboration); SET NULL so a
    # deprovisioned admin does not cascade-delete the tenant's live cap.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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
        # The message-hydration read path (#377 history reload) filters on
        # (tenant_id, message_id IN …) — Postgres does not index the FK
        # automatically, so without this every history fetch scans the tenant's
        # trace rows (#397).
        Index("ix_tool_invocations_tenant_message", "tenant_id", "message_id"),
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
    # DEFERRABLE INITIALLY DEFERRED: the chat runtime records a tool_invocations
    # row *during* the tool loop keyed to the pre-minted assistant message id,
    # but that message row is only INSERTed by `_persist` at the end of the SAME
    # transaction. The reference is a legitimate intra-transaction forward ref, so
    # the FK is validated at COMMIT (message present) rather than at flush (would
    # spuriously fail with an IntegrityError and abort every tool-using answer).
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"),
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
    # A short, user-safe result summary for the thread trace (#377 — "what it
    # returned"): produced by OUR tool handlers (e.g. "3 passages", "13 documents"),
    # never a raw payload or vendor string, and bounded at the write chokepoint
    # (the repository truncates). Null when the handler produced none (denials).
    result_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Per-message arrival order (#397). ``created_at``'s server default is the
    # TRANSACTION timestamp on Postgres, so every call in one answer turn ties —
    # the ordinal (assigned by the runner's per-turn counter) is what actually
    # guarantees the contract's oldest-first ordering.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Assistant(TenantScopedMixin, TimestampMixin, Base):
    """A saved, named, governed chat configuration — the mutable head (ADR-0011 §1, #211).

    Tenant- and owner-scoped (INV-1/INV-2): a caller sees only their own (or
    granted) assistants. An assistant is **not** a new engine — it is a saved
    version of the three inputs the chat runtime already consumes (``instructions``
    → system prompt, ``tool_allowlist`` → the CC-A allowed-tool set,
    ``knowledge_scope`` → the retrieval filter) plus a ``model`` default, an
    accountable ``owner_id`` + a ``backup_owner_id`` (required before publish, §4),
    an ``autonomy_level``, and a lifecycle ``status``.

    ``owner_id``/``backup_owner_id`` use ``ON DELETE SET NULL`` (not CASCADE) so
    deprovisioning a user leaves the row for admin reassignment rather than
    silently deleting a governed config (ADR-0011 §4 — orphan handling). The two
    enum domains are ``CheckConstraint``-pinned so a bad value can never be stored.
    Tenant-scoped like every table (INV-1); the ``0016`` migration puts it under
    the RLS backstop.
    """

    __tablename__ = "assistants"
    __table_args__ = (
        Index("ix_assistants_tenant_owner", "tenant_id", "owner_id"),
        CheckConstraint(
            "autonomy_level in ('suggest', 'draft', 'act_with_approval', 'act_auto')",
            name="ck_assistants_autonomy_level",
        ),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled')",
            name="ck_assistants_status",
        ),
        # Library-governance certification domain (E6-6, #217) — CHECK-pinned so a
        # bad value can never be stored, the same posture as the other enum columns.
        CheckConstraint(
            "certification_state in ('none', 'certified', 'deprecated')",
            name="ck_assistants_certification_state",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    backup_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL ⇒ the smart server default resolved fail-closed at run time.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The narrowing retrieval filter {collection_ids, source_ids, modes}.
    knowledge_scope: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    # The CC-A allowed-tool set (string[]). [] ⇒ the ad-hoc default set.
    tool_allowlist: Mapped[list[object]] = mapped_column(_JSON, nullable=False, default=list)
    autonomy_level: Mapped[str] = mapped_column(String(20), nullable=False, default="suggest")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    # --- Library governance (E6-6/E6-8, #217) — an orthogonal admin axis --------
    # Certification verdict (none|certified|deprecated); admin-only mutation.
    certification_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none", server_default="none"
    )
    # Whether an admin has featured/pinned the assistant in the library.
    featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # A free-text library grouping label (nullable ⇒ uncategorised).
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Set (in lockstep with status=disabled) when an admin disables the assistant so
    # the "only a published assistant may start" gate blocks it; NULL ⇒ not disabled.
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    versions: Mapped[list[AssistantVersion]] = relationship(
        back_populates="assistant", cascade="all, delete-orphan"
    )


class AssistantVersion(TenantScopedMixin, Base):
    """An immutable published snapshot — the ``assistant_versions`` row (ADR-0011 §1, #211).

    No ``TimestampMixin``/``updated_at``: a published version is **write-once**
    (append-only) — the app DB role is granted no UPDATE/DELETE on it (the ``0016``
    migration revokes them), the same posture as ``audit_events``. A session/run
    pins the exact ``id`` it ran, so editing or rolling back an assistant never
    rewrites what a past transcript ran on. ``config`` is the full frozen assistant
    definition; ``version`` is monotonic per assistant (``UNIQUE`` within it). A
    rollback appends a **new** version copying a prior ``config`` — never mutates
    history. Tenant-scoped (INV-1); the ``0016`` migration also puts it under the
    RLS backstop.
    """

    __tablename__ = "assistant_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "assistant_id",
            "version",
            name="uq_assistant_versions_assistant_version",
        ),
        Index("ix_assistant_versions_tenant_assistant", "tenant_id", "assistant_id"),
        CheckConstraint("version >= 1", name="ck_assistant_versions_version_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    assistant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistants.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Who published (or rolled back to create) this version. SET NULL so removing
    # that user does not cascade-delete the immutable snapshot.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    config: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    assistant: Mapped[Assistant] = relationship(back_populates="versions")


class Run(TenantScopedMixin, Base):
    """One execution of an assistant — scheduled or manual — the ``runs`` row (ADR-0015 §2, #235).

    The headless-run record: an assistant run that executes with **no WebSocket
    client present**, persisting a full transcript. Tenant- and owner-scoped
    (INV-1/INV-2): ``owner_id`` is the run's principal, so ``retrieval/`` admits
    exactly what that user could retrieve interactively — a headless run can never
    read what its runner can't. It pins the exact ``assistant_version_id`` executed
    (reproducible after edits) and links its cited answer through the shared
    ``messages`` chain (``message_id``), so INV-3 holds identically to interactive
    chat. ``schedule_id`` is null for a manual/run-now run. ``status`` walks the
    :class:`~app.domain.entities.RunStatus` state machine; the two enum domains are
    ``CheckConstraint``-pinned so a bad value can never be stored. Tenant-scoped
    like every table (INV-1); the ``0017`` migration puts it under the RLS backstop.

    No ``TimestampMixin``/``updated_at``: a run's lifecycle is expressed by
    ``started_at``/``finished_at`` + ``status``, not a generic updated stamp.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_tenant_owner", "tenant_id", "owner_id"),
        Index("ix_runs_tenant_assistant", "tenant_id", "assistant_id"),
        Index("ix_runs_tenant_schedule", "tenant_id", "schedule_id"),
        Index("ix_runs_tenant_status", "tenant_id", "status"),
        CheckConstraint("trigger in ('schedule', 'manual')", name="ck_runs_trigger"),
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'escalated')",
            name="ck_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The run's principal — retrieval runs *as* this user (INV-2). SET NULL on
    # user delete so a completed run's record survives deprovisioning (like the
    # assistant owner), never cascade-deleted.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    assistant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The pinned version executed — a run is reproducible after the assistant is
    # edited (E1 versioning). SET NULL so pruning history does not delete the run.
    assistant_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistant_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Null ⇒ a manual/run-now run. The FK → ``schedules`` lands with the scheduler
    # (#236, the ``0018`` migration): SET NULL so deleting a schedule leaves its
    # already-recorded runs (a run's history outlives the schedule that fired it).
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The internal chat session the headless runtime persists its message/citations
    # against (ADR-0015 §2 — "citations reuse the existing chain"). SET NULL so the
    # run record outlives a pruned session.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # The resolved input_params snapshotted for this fire.
    inputs: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    # The assistant message the transcript produced (the shared messages chain).
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Typed failure/escalation reason {code, message} — never a raw vendor string.
    error: Mapped[dict[str, object] | None] = mapped_column(_JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    steps: Mapped[list[RunStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RunStep(TenantScopedMixin, Base):
    """One ordered step of a run's persisted transcript — a ``run_steps`` row (ADR-0015 §2, #235).

    The durable analogue of the WS envelope stream: the :class:`RunTranscriptSink`
    appends one row per published chat envelope so an **unwatched** run's stream is
    fully reconstructable. ``seq`` is monotonic per run — the same ordering
    guarantee as the WS ``seq``; ``(run_id, seq)`` is UNIQUE so a step can never be
    duplicated or reordered. ``payload`` carries the envelope ``data`` typed per
    ``kind``. Tenant-scoped (INV-1); the ``0017`` migration puts it under RLS.

    No ``TimestampMixin``/``updated_at``: a transcript step is written once and
    never re-described (append-only, like an audit event).
    """

    __tablename__ = "run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),
        Index("ix_run_steps_tenant_run", "tenant_id", "run_id"),
        CheckConstraint("seq >= 0", name="ck_run_steps_seq_nonneg"),
        CheckConstraint(
            "kind in ('delta', 'tool_call', 'tool_result', 'citation', 'error')",
            name="ck_run_steps_kind",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="steps")


class Schedule(TenantScopedMixin, TimestampMixin, Base):
    """A user-defined recurring run of a saved assistant — a ``schedules`` row (ADR-0015 §2, #236).

    The dynamic scheduler's source of truth (Postgres authoritative; the derived
    RedBeat entry lives in Redis, ADR-0015 §1). Tenant- and owner-scoped
    (INV-1/INV-2): ``owner_id`` is the run's **principal** — a fired run retrieves
    only what the owner could retrieve interactively. ``assistant_id`` (CASCADE — a
    schedule is meaningless without its assistant) is the saved assistant to run;
    ``cadence`` is the normalized canonical 5-field cron string (a structured cadence
    is normalized to cron + the original preserved in ``cadence_structured`` for the
    UI); ``timezone`` (IANA) drives the DST-correct ``next_run_at``. ``enabled`` =
    firing; pausing sets it false (the derived RedBeat entry is removed).
    ``overlap_policy`` gates a fire while a prior run is still active (ADR-0015 §5).
    ``input_params``/``delivery`` are jsonb. ``last_run_at``/``last_status`` summarize
    the last fire for the list view. Tenant-scoped (INV-1); the ``0018`` migration
    puts it under the RLS backstop and adds the ``runs.schedule_id`` FK.
    """

    __tablename__ = "schedules"
    __table_args__ = (
        Index("ix_schedules_tenant_owner", "tenant_id", "owner_id"),
        Index("ix_schedules_tenant_assistant", "tenant_id", "assistant_id"),
        # The Beat reconcile sweep reads enabled schedules across the fleet; a
        # tenant-leading (enabled) index serves it and the owner list-view filter.
        Index("ix_schedules_tenant_enabled", "tenant_id", "enabled"),
        CheckConstraint(
            "overlap_policy in ('skip', 'queue', 'allow')",
            name="ck_schedules_overlap_policy",
        ),
        CheckConstraint(
            "last_status is null or "
            "last_status in ('queued', 'running', 'succeeded', 'failed', 'escalated')",
            name="ck_schedules_last_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The run's principal — retrieval runs *as* this user (INV-2). SET NULL on user
    # delete so a schedule survives deprovisioning for admin reassignment (like the
    # assistant owner), never cascade-deleted.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    assistant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assistants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The normalized canonical 5-field cron string (the stored recurrence form). A
    # structured cadence is normalized to this + preserved in ``cadence_structured``.
    cadence_cron: Mapped[str] = mapped_column(String(100), nullable=False)
    # The original structured cadence blob when the schedule was created that way
    # (null for a raw-cron schedule) — lets the UI round-trip the human form.
    cadence_structured: Mapped[dict[str, object] | None] = mapped_column(_JSON, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    input_params: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    delivery: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False, default=dict)
    overlap_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="skip")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Computed tz/DST-correct; null while paused (no upcoming fire).
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)


class RunDelivery(TenantScopedMixin, Base):
    """One in-app delivery of a completed run's output — a ``run_deliveries`` row (#238).

    The persistence of the run inbox (ADR-0015 §6): when a run reaches a terminal, a
    delivery lands here so the owner sees its ``summary`` + a link to the full cited
    transcript without opening the app. Tenant- and owner-scoped (INV-1/§2.2):
    ``recipient_id`` is the owner the run ran *as*, so a delivery in another tenant /
    addressed to another user is a 404 (existence non-disclosure, enforced one layer
    up in the service off the tenant-scoped repository's ``None``). ``run_id``
    (CASCADE — a delivery is meaningless without its run) links back to
    ``GET /runs/{id}``; the nullable ``schedule_id`` (SET NULL) records the schedule
    that fired it (null for a manual run). ``kind`` distinguishes an immediate inbox
    delivery from a digest-batched one; ``status`` walks the
    :class:`~app.domain.entities.RunDeliveryStatus` machine; both enum domains are
    ``CheckConstraint``-pinned so a bad value can never be stored. ``read_at`` stamps
    when the owner opened it. Tenant-scoped like every table (INV-1); the ``0023``
    migration puts it under the RLS backstop.

    No ``TimestampMixin``/``updated_at``: a delivery's lifecycle is ``created_at`` +
    ``read_at`` + ``status``, not a generic updated stamp.
    """

    __tablename__ = "run_deliveries"
    __table_args__ = (
        # The owner inbox reads (newest first) + the pending-digest sweep; each
        # tenant-leading so the equality filter uses one index (INV-1).
        Index("ix_run_deliveries_tenant_recipient", "tenant_id", "recipient_id"),
        Index("ix_run_deliveries_tenant_status", "tenant_id", "status"),
        Index("ix_run_deliveries_run_id", "run_id"),
        CheckConstraint("kind in ('inbox', 'digest')", name="ck_run_deliveries_kind"),
        CheckConstraint(
            "status in ('pending', 'delivered', 'read', 'failed')",
            name="ck_run_deliveries_status",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The owner the run ran as — the delivery recipient (INV-2/§2.2). SET NULL on
    # user delete so a delivery record survives deprovisioning, never cascade-deleted.
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    # CASCADE: a delivery is meaningless without its run.
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The schedule that fired the run (null for a manual run). SET NULL so the
    # delivery outlives the schedule that produced it (like ``runs.schedule_id``).
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="inbox")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="delivered")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CodeRun(TenantScopedMixin, Base):
    """One agent-authored sandbox code run — the ``code_runs`` row (ADR-0013 §4, #230).

    The durable record of a run of adversarial-by-assumption Python in the isolated
    sandbox: the exact ``code`` executed (inspectable, E15-7/E6-5), the pinned
    ``image_digest`` actually used (reproducibility, E3-7), captured (output-size-
    capped, G7) ``stdout``/``stderr``, ``exit_code``, ``duration_ms``, measured
    ``resource_usage`` (jsonb), and the ``artifact_ids`` of any output files
    collected via CC-B (#208). Tenant- and owner-scoped (INV-1/INV-2): a run in
    another tenant / owned by another user is a 404 (existence non-disclosure). The
    nullable ``session_id`` (a real FK — its table exists, SET NULL) links a run
    fired inside a chat turn; ``run_id`` / ``trace_id`` are plain nullable UUIDs
    (their referent tables are the run/agent-trace framework — they become FKs when
    those land, mirroring ``artifacts``).

    ``status`` walks the :class:`~app.domain.entities.CodeRunStatus` machine; a
    crash-safe task always writes a terminal, never a stuck ``running`` (ADR-0013 §5,
    INV-8). No ``TimestampMixin``/``updated_at``: the lifecycle is ``created_at`` →
    ``started_at`` → ``finished_at`` + ``status``. Tenant-scoped like every table
    (INV-1); the ``0019`` migration puts it under the RLS backstop.
    """

    __tablename__ = "code_runs"
    __table_args__ = (
        Index("ix_code_runs_tenant_owner", "tenant_id", "owner_id"),
        Index("ix_code_runs_tenant_status", "tenant_id", "status"),
        Index("ix_code_runs_session_id", "session_id"),
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'timeout', "
            "'killed', 'denied')",
            name="ck_code_runs_status",
        ),
        CheckConstraint(
            "exit_code is null or exit_code >= 0", name="ck_code_runs_exit_code_nonneg"
        ),
        CheckConstraint(
            "duration_ms is null or duration_ms >= 0", name="ck_code_runs_duration_nonneg"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    # The run's principal — the sandbox runs code *on this user's behalf* (INV-2).
    # SET NULL on user delete so a completed run's record survives deprovisioning.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    # The chat session the run fired inside (only real link FK; its table exists).
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The parent agent run / trace — plain nullable UUIDs until those tables land.
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # The exact Python source executed — inspectable (E15-7/E6-5), reproducible.
    code: Mapped[str] = mapped_column(Text, nullable=False)
    # Captured, output-size-capped (G7). Default empty (never null) so a queued run
    # already has readable (empty) output fields.
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Measured peak mem / cpu time / max pids / output bytes (best-effort).
    resource_usage: Mapped[dict[str, object] | None] = mapped_column(_JSON, nullable=True)
    # The pinned base-image digest actually used (reproducibility, E3-7).
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Ids of artifacts the run emitted (collected via CC-B, tenant-scoped). Stored as
    # a portable string array; default empty until the run produces output files.
    artifact_ids: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
