"""Domain entities for the relational schema (spec 0004 §4).

Pure, frozen dataclasses — **no ORM, no SQLAlchemy, no framework imports**
(backend/AGENTS.md: ``domain/`` is pure). The ``db/`` repositories return *these*
to their callers, never an ORM row or a ``Session`` (ADR-0004 boundary rule:
adapters expose domain types). When the persistence shape changes, the mapping
in ``db/`` moves and nothing upstream does.

Each entity mirrors a row of the corresponding table. Tenant-scoped entities
carry ``tenant_id``; ownership-bearing ones also carry ``owner_id`` (spec 0004
§2.1/§2.2). The wire shapes in ``contracts/openapi.yaml`` are a *projection* of
these (e.g. ``Collection.document_count`` is computed, not stored); services map
domain → wire, so these stay storage-faithful.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class Role(str, enum.Enum):
    """RBAC roles (spec 0004 §2.3). A user may hold several."""

    MEMBER = "member"
    ADMIN = "admin"
    SECURITY = "security"


class MessageRole(str, enum.Enum):
    """Author of a chat message (contracts/openapi.yaml MessageRole)."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class DocumentStatus(str, enum.Enum):
    """Ingestion lifecycle (contracts/openapi.yaml DocumentStatus)."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class SourceStatus(str, enum.Enum):
    """Connector sync lifecycle (contracts/openapi.yaml SourceStatus, ADR-0009 §4).

    ``pending`` = added, first sync not yet run; ``syncing`` = a sync is in
    flight; ``ready`` = the last sync succeeded and content is indexed;
    ``error`` = the last sync failed (see ``last_error``).
    """

    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    ERROR = "error"


class WebSourceMode(str, enum.Enum):
    """How a ``web`` source's URL is interpreted (contracts/openapi.yaml, ADR-0009 §2).

    ``page`` = one URL → one document; ``feed`` = an RSS/Atom feed → many
    documents (bounded); ``sitemap`` = a sitemap.xml → many documents (bounded).
    The server detects the mode from the fetched content.
    """

    PAGE = "page"
    FEED = "feed"
    SITEMAP = "sitemap"


class AuditOutcome(str, enum.Enum):
    """Outcome of an audited action (spec 0004 §2.4)."""

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


class GrantResourceType(str, enum.Enum):
    """What kind of resource an explicit grant is on (spec 0004 §2.2).

    A grant on a ``COLLECTION`` cascades to every document in it; a grant on a
    ``DOCUMENT`` is on that one document. These are the MVP resources; the column
    is sized to admit more later without a schema change.
    """

    COLLECTION = "collection"
    DOCUMENT = "document"


class GrantPrincipalType(str, enum.Enum):
    """Who an explicit grant is *to* (spec 0004 §2.2).

    The MVP only issues ``USER`` grants (a grantee user id). ``GROUP``/``ROLE``
    are modelled now — the column admits them — so group/role sharing lands later
    as a service change, not a migration.
    """

    USER = "user"
    GROUP = "group"
    ROLE = "role"


class GrantRole(str, enum.Enum):
    """The access level an explicit grant confers (spec 0004 §2.2).

    The MVP confers read access only (``VIEWER``) — the whole product is T0/read
    (spec 0004 §2.5), so a grant never carries a write capability. Richer roles
    are a later extension; the column admits them.
    """

    VIEWER = "viewer"


@dataclass(frozen=True, slots=True)
class Tenant:
    """A customer boundary. The root of every isolation predicate (INV-1)."""

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    # Per-tenant override of the chat agent's tool-use-turn budget (issue #148).
    # ``None`` ⇒ use the system default (``Settings.chat_max_tool_turns``); when
    # set it caps how many tool-calling turns the answer runtime may take before
    # it forces a final synthesis (1–50). A tenant admin configures it.
    max_tool_turns: int | None = None


@dataclass(frozen=True, slots=True)
class User:
    """An app-managed principal bound to exactly one tenant (spec 0004 §2.3)."""

    id: UUID
    tenant_id: UUID
    email: str
    password_hash: str
    roles: tuple[Role, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """A rotating, revocable refresh-token record (spec 0004 §2.3).

    The opaque token itself is never stored — only its hash (``token_hash``), so
    a DB leak does not yield usable tokens. ``revoked_at`` set ⇒ unusable
    (logout or rotation); ``expires_at`` caps its lifetime. One row per issued
    token; refresh rotates by revoking the presented row and issuing a new one.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    token_hash: str
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Collection:
    """A folder grouping a user's documents. Ownership-bearing (spec 0004 §2.2)."""

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Document:
    """An uploaded file. Ownership-bearing; ingested into ``chunks`` (#21)."""

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    collection_id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    storage_key: str
    status: DocumentStatus
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Source:
    """A connected external source ingested by a connector (ADR-0009 §4).

    Tenant- and owner-scoped, deny-by-default: only the adding user (within
    their tenant) can retrieve what a source ingests (INV-1/INV-2). ``type`` is
    the connector name (e.g. ``web``); ``config`` is the connector's opaque,
    portable JSON (e.g. ``{"url": ..., "mode": ...}``). ``status`` tracks the
    sync lifecycle; ``indexed_count`` is how many documents the last sync
    produced, ``last_error`` the failure detail. Ingested ``documents`` link
    back via ``source_id``; deleting a source removes them (and the auto-created
    backing collection when it holds nothing else) via the sources service — see
    :meth:`~app.services.sources_service.SourcesService.delete`.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    type: str
    config: dict[str, object]
    status: SourceStatus
    indexed_count: int
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable passage of a document, with its embedding in-row.

    The ``embedding`` lives beside ``tenant_id`` (and the document FK) so
    permission-aware retrieval is one ``WHERE`` clause (backend/AGENTS.md, spec
    0004 §4). ``char_start``/``char_end`` carry the source span for citations
    (CC-11). The vector is exposed as a plain ``list[float]`` — no pgvector type
    crosses the boundary.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    ord: int
    text: str
    embedding: tuple[float, ...] | None
    char_start: int
    char_end: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatSession:
    """A conversation thread. Ownership-bearing (spec 0004 §2.2)."""

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    title: str
    model: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """A user's account preferences (spec 0005). One row per user, created lazily
    on the first write — a fresh user has none, and the service then returns the
    implicit server-default state (``default_model`` ``None``). Tenant-scoped."""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    default_model: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SavedSearch:
    """A saved ``/search`` query + its optional filters (spec 0005, epic #144).

    Ownership-bearing (spec 0004 §2.2): a caller only ever sees their own.
    ``query`` + ``collection_id`` / ``source`` / ``type`` capture exactly what
    ``/search`` accepts, so applying one re-runs the same search.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    query: str
    collection_id: UUID | None
    source: str | None
    type: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecentSearch:
    """A recent ``/search`` query the caller ran (spec 0005, epic #144).

    The wire projection: the display query + when it was last used. De-duplicated
    per ``(tenant, user, normalized query)`` and capped per user by the repository.
    """

    query: str
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a chat session. Assistant turns may carry citations."""

    id: UUID
    tenant_id: UUID
    session_id: UUID
    role: MessageRole
    content: str
    model: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Citation:
    """A passage-level reference from an assistant message (INV-3).

    Points at a ``chunk`` the caller was permitted to retrieve, with the span
    within it. ``char_start``/``char_end`` are message-relative offsets the UI
    renders as a clickable reference (contracts/openapi.yaml Citation).
    """

    id: UUID
    tenant_id: UUID
    message_id: UUID
    chunk_id: UUID
    char_start: int
    char_end: int
    score: float | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One append-only product-audit record (spec 0004 §2.4).

    Required fields per §2.4; ``metadata`` carries event-specific extras
    (query hash, retrieved document ids, model id, citation count for
    ``retrieval.query``/``answer.generated``). The table grants the app role no
    ``UPDATE``/``DELETE`` (enforced in the migration) — append-only by design.
    """

    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    request_id: str | None
    source_ip: str | None
    metadata: dict[str, object] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.min)


@dataclass(frozen=True, slots=True)
class Grant:
    """An explicit access grant — the ``grants`` table row (spec 0004 §2.2, CC-1).

    Records that ``principal`` (MVP: a user, ``principal_type=user`` +
    ``principal_id``) may access ``resource`` (a ``collection`` or ``document``,
    by id) the requester does not own, at ``role`` (MVP: ``viewer``). The
    retrieval permission filter widens to admit a row whose owner is the requester
    **or** for which such a grant exists (INV-2); a collection grant cascades to
    its documents. Tenant-scoped (INV-1): a grant only ever applies within its own
    tenant. ``granted_by`` is the owner/admin who created it (audited).
    """

    id: UUID
    tenant_id: UUID
    resource_type: GrantResourceType
    resource_id: UUID
    principal_type: GrantPrincipalType
    principal_id: UUID
    role: GrantRole
    granted_by: UUID | None
    created_at: datetime
