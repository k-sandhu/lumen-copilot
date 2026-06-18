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


class AuditOutcome(str, enum.Enum):
    """Outcome of an audited action (spec 0004 §2.4)."""

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Tenant:
    """A customer boundary. The root of every isolation predicate (INV-1)."""

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


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
