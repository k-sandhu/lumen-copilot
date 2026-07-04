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

from app.domain.scheduling import Cadence


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


class ArtifactProducedBy(str, enum.Enum):
    """What produced an artifact (issue #208) — the write origin.

    An artifact is a file an agent/run *produced* (distinct from a user-uploaded
    ``document``). ``CHAT_SESSION`` = written during an interactive chat turn,
    ``RUN`` = produced by a background run, ``TOOL`` = emitted by a tool/code
    invocation. The value is fixed at creation from the producing context, never
    from request input, and pins which nullable link id (``session_id`` /
    ``run_id`` / ``tool_invocation_id``) the row carries.
    """

    CHAT_SESSION = "chat_session"
    RUN = "run"
    TOOL = "tool"


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


class SecretKind(str, enum.Enum):
    """What a stored secret is *for* (issue #209).

    The consumer seam: ``MCP_AUTH`` is an MCP server's auth token/header (E3),
    ``SEARCH_API`` a hosted web-search provider key (SPIKE-4), ``OTHER`` a
    forward-compatible catch-all for future connector/action credentials. The
    column admits more kinds without a schema change.
    """

    MCP_AUTH = "mcp_auth"
    SEARCH_API = "search_api"
    OTHER = "other"


class McpServerStatus(str, enum.Enum):
    """Health state of a registered MCP server (ADR-0012 §5, contract ``McpServerStatus``).

    ``PENDING`` before the first successful probe (a freshly registered server),
    ``READY`` after a healthy handshake, ``ERROR`` when the last probe failed
    (see ``last_error``). Mirrors the ``mcp_servers.status`` CHECK domain and the
    frozen ``McpServerStatus`` contract enum.
    """

    PENDING = "pending"
    READY = "ready"
    ERROR = "error"


class AutonomyLevel(str, enum.Enum):
    """How much an assistant may act on its own (ADR-0011 §3, contract ``AutonomyLevel``).

    Capped by the risk tier of the assistant's allowed tools; at MVP the whole
    product is T0/T1, so autonomy governs only app-local tools through the CC-A
    approval seam. ``SUGGEST`` is the default for a new assistant.

    The four values are **totally ordered** by how far the assistant may act
    (``SUGGEST`` < ``DRAFT`` < ``ACT_WITH_APPROVAL`` < ``ACT_AUTO``) so an admin's
    per-tenant cap can be a ``min`` (issue #218): an assistant's EFFECTIVE autonomy
    is ``min(assistant.autonomy_level, tenant cap)``, and a T1 side-effecting tool
    is gated by that effective level at run time.
    """

    SUGGEST = "suggest"
    DRAFT = "draft"
    ACT_WITH_APPROVAL = "act_with_approval"
    ACT_AUTO = "act_auto"

    @property
    def rank(self) -> int:
        """The numeric autonomy rank (0–3) — higher means the assistant may act further.

        ``SUGGEST`` = 0 … ``ACT_AUTO`` = 3. Lets a cap be expressed as a ``min`` and a
        run-time gate as a ``>=`` comparison without a lookup table (issue #218).
        """
        return _AUTONOMY_ORDER.index(self)

    def clamped_to(self, cap: AutonomyLevel) -> AutonomyLevel:
        """This level, lowered to ``cap`` if it exceeds it (the tenant-cap ``min``).

        The EFFECTIVE autonomy of an assistant is ``min(assistant, tenant cap)``: an
        assistant configured above the tenant ceiling runs at the ceiling, never above
        it (issue #218). Returns ``self`` when already at or below the cap.
        """
        return self if self.rank <= cap.rank else cap


# The autonomy values in ascending order (SUGGEST lowest, ACT_AUTO highest). The
# single source for ``AutonomyLevel.rank`` so the ordering is defined once.
_AUTONOMY_ORDER: tuple[AutonomyLevel, ...] = (
    AutonomyLevel.SUGGEST,
    AutonomyLevel.DRAFT,
    AutonomyLevel.ACT_WITH_APPROVAL,
    AutonomyLevel.ACT_AUTO,
)


class AssistantStatus(str, enum.Enum):
    """Assistant lifecycle (ADR-0011 §1, contract ``AssistantStatus``).

    ``DRAFT`` = the working head, never published; ``PUBLISHED`` = has at least
    one immutable version and may start sessions; ``DISABLED`` = cannot start new
    sessions (an orphaned/retired assistant).
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class CertificationState(str, enum.Enum):
    """Admin library-governance certification (E6-6, issue #217, contract ``CertificationState``).

    An **orthogonal** governance axis to the lifecycle ``status`` (a certified
    assistant is still ``published``/``disabled`` on its own axis). ``NONE`` = not
    reviewed; ``CERTIFIED`` = an admin vouched for it (trusted in the library);
    ``DEPRECATED`` = an admin flagged it for retirement (still runnable, but the
    library warns). Only an admin transitions this (deny-by-default, INV-5); the
    default for a fresh assistant is ``NONE``.
    """

    NONE = "none"
    CERTIFIED = "certified"
    DEPRECATED = "deprecated"


class KnowledgeMode(str, enum.Enum):
    """A retrieval source class an assistant's scope may draw from (contract ``KnowledgeMode``)."""

    COMPANY = "company"
    UPLOADED = "uploaded"
    WEB = "web"
    MODEL = "model"


class RunTrigger(str, enum.Enum):
    """Why an assistant run fired (ADR-0015 §2, contract ``RunTrigger``).

    ``SCHEDULE`` = a cadence fire (the scheduler #236 enqueues it); ``MANUAL`` =
    a run-now (the API / test harness enqueues it). A run always records exactly
    one trigger so the audit trail shows *why* it ran.
    """

    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunStatus(str, enum.Enum):
    """The run state machine (ADR-0015 §2, contract ``RunStatus``).

    ``QUEUED`` → ``RUNNING`` → one terminal of ``SUCCEEDED`` / ``FAILED`` /
    ``ESCALATED``. ``ESCALATED`` is a first-class terminal distinct from
    ``FAILED`` (the run needs a human, ADR-0015 §6). A crash never leaves a run
    stuck ``RUNNING``: the Celery task's failure path writes ``FAILED`` with a
    typed error (INV-8 — an illegal transition is rejected). ``#239`` owns the
    escalation triggers; this epic fixes only that ``escalated`` is a status.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"


# The terminal run statuses — a run in one of these will never transition again
# (the exactly-one-terminal contract, mirroring the chat runtime's WS lifecycle).
_TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ESCALATED}
)


class CodeRunStatus(str, enum.Enum):
    """The sandbox code-run state machine (ADR-0013 §4, contract ``CodeRunStatus``).

    ``QUEUED`` (enqueued, not yet started) → ``RUNNING`` (executing in the ephemeral
    container) → one terminal of ``SUCCEEDED`` (exit 0) / ``FAILED`` (non-zero exit) /
    ``TIMEOUT`` (wall-clock-killed, G6) / ``KILLED`` (OOM-killed or pids-capped, G6) /
    ``DENIED`` (refused **before** execution — code execution disabled for the tenant,
    a policy/quota block, §6). A crash never leaves a run stuck ``RUNNING``: the Celery
    task's failure path writes ``FAILED`` with a typed error (INV-8 — the sandbox never
    ends in silence, ADR-0013 §5).
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    KILLED = "killed"
    DENIED = "denied"


# The terminal code-run statuses — a run in one of these never transitions again.
_TERMINAL_CODE_RUN_STATUSES: frozenset[CodeRunStatus] = frozenset(
    {
        CodeRunStatus.SUCCEEDED,
        CodeRunStatus.FAILED,
        CodeRunStatus.TIMEOUT,
        CodeRunStatus.KILLED,
        CodeRunStatus.DENIED,
    }
)


class OverlapPolicy(str, enum.Enum):
    """What happens when a schedule fires while its prior run is still active (ADR-0015 §5).

    ``SKIP`` (the default) records a skipped fire and enqueues nothing — so a slow
    daily run never stacks up; ``QUEUE`` enqueues behind the active run; ``ALLOW``
    runs concurrently. The dispatcher checks the schedule's active-run count against
    this before enqueuing (INV-8: the overlap gate is a policy, not an accident).
    """

    SKIP = "skip"
    QUEUE = "queue"
    ALLOW = "allow"


class DigestCadence(str, enum.Enum):
    """How often completed runs roll into an in-app digest (ADR-0015 §6, contract).

    v1 delivery is in-app only (no external egress, INV-7): ``NONE`` = no digest,
    ``DAILY``/``WEEKLY`` = periodic in-app roll-up. The delivery detail is #238; this
    fixes only the shape the schedule stores.
    """

    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class ScheduleDelivery:
    """Where a completed run's result lands (ADR-0015 §6, contract ``ScheduleDelivery``).

    v1 in-app only: ``inbox`` lands the run in the owner's in-app run inbox;
    ``digest`` optionally rolls recent runs into a periodic in-app notification.
    External channels (email/Slack) are deferred (T2-ish egress, INV-7 — #238).
    """

    inbox: bool = True
    digest: DigestCadence | None = None

    @classmethod
    def default(cls) -> ScheduleDelivery:
        """The v1 default delivery: land in the inbox, no digest."""
        return cls(inbox=True, digest=None)


class RunStepKind(str, enum.Enum):
    """A run-transcript step kind (ADR-0015 §2, contract ``RunStepKind``).

    The durable analogue of the WS envelope type set: ``DELTA`` (incremental
    output), ``TOOL_CALL`` / ``TOOL_RESULT`` (a tool turn), ``CITATION`` (a
    grounded passage), ``ERROR`` (a failure step). The :class:`RunTranscriptSink`
    maps each published chat envelope to one of these so an unwatched run's
    stream is fully reconstructable from ``run_steps``.
    """

    DELTA = "delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CITATION = "citation"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """The assistant's retrieval filter (ADR-0011 §1/§2, contract ``KnowledgeScope``).

    A **narrowing** set layered on top of the per-user INV-2 permission predicate
    inside ``retrieval/`` — it can only *intersect*, never widen, what the running
    principal may already retrieve. Empty lists ⇒ no narrowing on that axis.
    ``collection_ids``/``source_ids`` scope retrieval to owned/granted collections
    or connected sources; ``modes`` selects which source classes the scope draws
    from.
    """

    collection_ids: tuple[UUID, ...] = ()
    source_ids: tuple[UUID, ...] = ()
    modes: tuple[KnowledgeMode, ...] = ()

    @classmethod
    def empty(cls) -> KnowledgeScope:
        """The no-narrowing scope (a fresh draft's default)."""
        return cls()


@dataclass(frozen=True, slots=True)
class Assistant:
    """A saved, named, governed chat configuration — the ``assistants`` head row (#211).

    The **mutable head** / working definition (ADR-0011 §1): the three inputs the
    chat runtime already consumes (``instructions`` → system prompt,
    ``tool_allowlist`` → the CC-A allowed-tool set, ``knowledge_scope`` → the
    retrieval filter) plus a ``model`` default, an accountable ``owner_id`` +
    a ``backup_owner_id`` (required before publish), an ``autonomy_level``, and a
    lifecycle ``status``. Tenant- and owner-scoped (INV-1/INV-2): a caller sees
    only their own (or granted) assistants; a cross-tenant/non-owned id is 404.
    ``current_version`` is the published head version number (``None`` while never
    published) — a projection the service fills, not a stored column.
    ``effective_autonomy`` is the assistant's autonomy after the tenant admin cap is
    applied (``min(autonomy_level, tenant cap)``, issue #218) — also a projection the
    service fills (``None`` until filled), so the library/run detail can show how far
    the assistant may *actually* act versus its configured level.

    **Library governance (E6-6/E6-8, issue #217 — an orthogonal admin axis).**
    ``certification_state`` (an admin's certify/deprecate verdict),
    ``featured`` (an admin's library pin), ``category`` (a library grouping label),
    and ``disabled_at`` (when an admin disabled it — set in lockstep with
    ``status=disabled`` so the existing run/chat/schedule "only a published
    assistant may start" gate blocks a disabled assistant, INV-8). ``owner_orphaned``
    is a **projection** (not stored): whether the owner is no longer a member of the
    tenant, so the library can flag an abandoned assistant for admin reassignment.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    backup_owner_id: UUID | None
    name: str
    description: str | None
    instructions: str | None
    model: str | None
    knowledge_scope: KnowledgeScope
    tool_allowlist: tuple[str, ...]
    autonomy_level: AutonomyLevel
    status: AssistantStatus
    certification_state: CertificationState
    featured: bool
    category: str | None
    disabled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    current_version: int | None = None
    effective_autonomy: AutonomyLevel | None = None
    owner_orphaned: bool = False


@dataclass(frozen=True, slots=True)
class AssistantVersion:
    """An immutable published snapshot — an ``assistant_versions`` row (#211).

    Write-once (append-only): the full frozen definition a session/run pins to
    (ADR-0011 §1). ``config`` is the complete assistant config at publish time; a
    rollback creates a **new** version whose ``config`` copies a prior one — history
    is never mutated or deleted. ``author_id`` is who published (or rolled back to
    create) it; ``notes``/``diff_summary`` are advisory release metadata.
    """

    id: UUID
    tenant_id: UUID
    assistant_id: UUID
    version: int
    author_id: UUID | None
    config: dict[str, object]
    notes: str | None
    diff_summary: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Secret:
    """One stored credential — the full ``secrets`` row (issue #209).

    Tenant- and owner-scoped (INV-1/§2.2). **No field holds plaintext**: the
    credential lives only as ``ciphertext`` + ``nonce`` under ``key_version``
    (envelope encryption, ``app.core.crypto``); ``hint`` is a non-reversing masked
    tail (e.g. the last four characters) for the UI. This full entity — including
    the ciphertext bytes — is returned by the repository *inside the service
    only*; the service hands callers the plaintext-free :class:`SecretRef` for
    metadata/list, and decrypts to a bare ``str`` solely for an in-process
    adapter's ``get_secret_plaintext`` (never a router).
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    kind: SecretKind
    ciphertext: bytes
    nonce: bytes
    key_version: int
    hint: str
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SecretRef:
    """The plaintext-free projection of a secret (issue #209 — the safe shape).

    What ``store_secret`` returns and ``list_secrets`` yields: identity +
    metadata + the masked ``hint``, and **never** the value, the ciphertext, or
    the nonce. This is the only secret shape any HTTP surface may serialize (AC-1,
    AC-3); the plaintext is reachable exclusively through the service's internal
    ``get_secret_plaintext`` for in-process adapters.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    kind: SecretKind
    hint: str
    created_at: datetime
    updated_at: datetime


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
class McpServer:
    """A registered remote MCP server — the full ``mcp_servers`` row (ADR-0012 §5).

    Tenant- and owner-scoped, deny-by-default: only the registering user (within
    their tenant) can retrieve/manage it (INV-1/INV-2). ``transport`` is the remote
    transport (``streamable_http``/``sse`` — stdio is impossible by type);
    ``endpoint_url`` is the https endpoint (SSRF-checked on register and every
    connect). ``auth_secret_ref`` is the id of a CC-C secret (a ``SecretRef.id``
    stringified) — **never the credential itself**; ``None`` for an anonymous
    server. ``secret_hint`` is the masked tail projected from that secret for the
    UI (also never the value). ``status`` tracks the health lifecycle;
    ``last_health_at`` is the last successful probe, ``last_error`` the last safe
    failure reason; ``discovered_tools`` is the last ``list_tools()`` snapshot
    (names + schemas + read-only annotations), stored as portable JSON.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    name: str
    transport: str
    endpoint_url: str
    auth_secret_ref: str | None
    enabled: bool
    status: McpServerStatus
    last_health_at: datetime | None
    last_error: str | None
    discovered_tools: list[dict[str, object]]
    secret_hint: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TenantToolPolicy:
    """An admin's per-tenant override of one tool's governance (issue #223).

    One row of the ``tenant_tool_policy`` table: is ``tool_name`` ``enabled`` for
    the tenant, and does an invocation still ``requires_approval``. Tenant-scoped
    (INV-1); ``updated_by`` is the admin who last set it (may be ``None`` if that
    user was later deprovisioned — the override outlives them). Absence of a row
    (not represented here) means the tool's built-in default applies — the
    deny-by-default rule the service and the approval gate enforce.
    """

    id: UUID
    tenant_id: UUID
    tool_name: str
    enabled: bool
    requires_approval: bool
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TenantSandboxPolicy:
    """An admin's per-tenant policy for the code-execution sandbox (issue #233).

    One row of the ``tenant_sandbox_policy`` table: is code execution ``enabled`` for
    the tenant, the package allow/deny lists, the outbound egress posture
    (``egress_allowed`` + ``egress_allowlist``), and the runtime / memory / quota caps.
    Tenant-scoped (INV-1); ``updated_by`` is the admin who last set it (may be ``None``
    if that user was later deprovisioned — the policy outlives them). Absence of a row
    (not represented here) means code execution stays DISABLED for the tenant — the
    deny-by-default rule the enforcement path enforces. The stored caps are CEILINGS
    the enforcement path clamps to the deploy-wide ``SANDBOX_*`` config (a per-tenant
    value only ever narrows, never widens).
    """

    id: UUID
    tenant_id: UUID
    enabled: bool
    allowed_packages: tuple[str, ...]
    denied_packages: tuple[str, ...]
    egress_allowed: bool
    egress_allowlist: tuple[str, ...]
    max_runtime_s: int
    max_memory_mb: int
    daily_runtime_cap_s: int
    max_concurrency: int
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TenantAutonomyPolicy:
    """An admin's per-tenant autonomy cap (issue #218).

    One row of the ``tenant_autonomy_policy`` table: the maximum
    :class:`AutonomyLevel` any assistant in the tenant may effectively run at
    (``max_autonomy``). Tenant-scoped (INV-1); ``updated_by`` is the admin who last
    set it (may be ``None`` if that user was later deprovisioned — the cap outlives
    them). Absence of a row (not represented here) means **no ceiling** — an
    assistant runs at its own configured level (the permissive default, mirroring
    how the tool/sandbox policies fall back to their built-in defaults). An
    assistant's EFFECTIVE autonomy is ``min(assistant.autonomy_level, max_autonomy)``,
    enforced at publish time and at run time.
    """

    id: UUID
    tenant_id: UUID
    max_autonomy: AutonomyLevel
    updated_by: UUID | None
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
    """A conversation thread. Ownership-bearing (spec 0004 §2.2).

    ``assistant_id``/``assistant_version_id`` are the optional assistant pin
    (ADR-0011 §1): a session started from an assistant records both the assistant
    and the exact published version it ran, so the transcript is reproducible after
    the assistant is edited/rolled back. Both ``None`` ⇒ ad-hoc chat (the default,
    unchanged path).
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    title: str
    model: str
    created_at: datetime
    updated_at: datetime
    assistant_id: UUID | None = None
    assistant_version_id: UUID | None = None


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
class Artifact:
    """A file an agent/run produced — the ``artifacts`` table row (issue #208).

    Distinct from a user-uploaded :class:`Document`: this is the persistence seam
    for the file-writing tool and the code sandbox's output files (CC-12). Tenant-
    and owner-scoped, deny-by-default (spec 0004 §2.1/§2.2, INV-1/INV-2): a
    caller sees only their own artifacts (plus any explicitly granted). Immutable
    — a new version is a new row (no ``updated_at``). ``produced_by`` records the
    write origin and pins which nullable link id is set (``session_id`` /
    ``run_id`` / ``tool_invocation_id``). ``storage_key`` is the tenant-prefixed,
    content-addressed object key under the ``artifacts/`` prefix
    (:mod:`app.storage.keys`); ``sha256`` is that content address.
    ``retention_expires_at`` (nullable = keep) is when a janitor may purge it.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    produced_by: ArtifactProducedBy
    filename: str
    mime_type: str
    size_bytes: int
    storage_key: str
    sha256: str
    created_at: datetime
    session_id: UUID | None = None
    run_id: UUID | None = None
    tool_invocation_id: UUID | None = None
    retention_expires_at: datetime | None = None


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


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One governed tool-call record — the ``tool_invocations`` table row (CC-7 #207).

    The durable trace of a single tool invocation through the governed registry
    (issue #207 §4): which tool ran in which session, for which assistant message,
    whether it succeeded, and how long it took. Written **per invocation** — a
    governance denial (off-allow-list / unapproved) and a tool failure are recorded
    too, with ``ok=False`` + the ``error`` code — so the trace never has a silent
    gap (feeds the WS trace now and AgentOps analytics later, E14).

    Tenant-scoped (INV-1). ``args_hash`` is a stable non-reversible hash of the
    call arguments (not the raw args — same discipline as the audit query hash,
    spec 0004 §2.4) so a reviewer can correlate identical calls without storing
    potentially sensitive argument text. ``run_id`` is reserved for a future
    multi-step agent-run grouping; the MVP leaves it ``None``.
    """

    id: UUID
    tenant_id: UUID
    session_id: UUID | None
    message_id: UUID | None
    tool_name: str
    args_hash: str
    ok: bool
    error: str | None
    duration_ms: int
    run_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.min)


@dataclass(frozen=True, slots=True)
class RunError:
    """A typed run failure / escalation reason — the ``runs.error`` jsonb (ADR-0015 §2).

    Never a raw vendor string (the runtime's opaque-error rule): ``code`` is a
    stable machine-readable reason (e.g. ``model_unavailable``, ``ambiguous``,
    ``tool_failed``) and ``message`` a human line for the owner's inbox. Present
    only on ``failed`` / ``escalated`` runs; the contract ``RunError`` shape.
    """

    code: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, raw: object) -> RunError | None:
        """Rebuild from a stored jsonb blob (``None`` / malformed ⇒ ``None``)."""
        if not isinstance(raw, dict):
            return None
        code = raw.get("code")
        message = raw.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            return None
        return cls(code=code, message=message)


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of an assistant — scheduled or manual — the ``runs`` row (ADR-0015 §2).

    Tenant- and owner-scoped (INV-1/INV-2): a run in another tenant or owned by
    another user is a 404 (existence non-disclosure). It pins the exact
    ``assistant_version_id`` executed so a run is reproducible after the assistant
    is edited (E1 versioning). ``status`` walks the :class:`RunStatus` state
    machine; a headless run persists its cited answer through the *same* messages
    /citations chain (``message_id`` links it), so INV-3 holds identically to
    interactive chat. ``id`` doubles as the WS ``streamId`` for live attach
    (ADR-0015 §3). ``owner_id`` is the run's principal — retrieval runs *as* them.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    assistant_id: UUID
    assistant_version_id: UUID | None
    schedule_id: UUID | None
    session_id: UUID | None
    trigger: RunTrigger
    status: RunStatus
    inputs: dict[str, object]
    summary: str | None
    message_id: UUID | None
    error: RunError | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        """True once the run has reached a terminal status (never transitions again)."""
        return self.status in _TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class RunStep:
    """One ordered step of a run's persisted transcript — a ``run_steps`` row (ADR-0015 §2).

    The durable equivalent of a WS envelope: ``seq`` is monotonic per run (the
    same ordering guarantee as the WS ``seq``; ``(run_id, seq)`` is unique), and
    ``payload`` carries the envelope ``data`` typed per ``kind``. Reconstructs
    exactly what a live watcher would have seen for an unwatched run.
    """

    id: UUID
    tenant_id: UUID
    run_id: UUID
    seq: int
    kind: RunStepKind
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Schedule:
    """A user-defined recurring run of a saved assistant — the ``schedules`` row (ADR-0015 §2).

    Tenant- and owner-scoped (INV-1/INV-2): a schedule in another tenant or owned by
    another user is a 404 (existence non-disclosure). ``owner_id`` is the run's
    **principal** — a fired run retrieves only what the owner could retrieve
    interactively (INV-2), the load-bearing security property of the epic.
    ``cadence`` (normalized to a canonical cron) + ``timezone`` (IANA) drive the
    DST-correct ``next_run_at`` (null while paused). ``enabled`` = firing; pausing
    sets it false and removes the derived RedBeat entry. ``last_run_at`` /
    ``last_status`` summarize the last fire for the list view.

    ``Cadence``/``ScheduleDelivery`` are imported from ``app.domain.scheduling`` /
    this module; the repository maps the stored jsonb ⇄ these.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    assistant_id: UUID
    cadence: Cadence
    timezone: str
    input_params: dict[str, object]
    delivery: ScheduleDelivery
    overlap_policy: OverlapPolicy
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: RunStatus | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Measured resource consumption for a finished code run (ADR-0013 §4, contract).

    Best-effort — a field is ``None`` when the runtime did not report it. Stored as
    the ``code_runs.resource_usage`` jsonb and projected to the contract
    ``ResourceUsage`` shape. ``output_bytes`` is the captured stdout+stderr size
    (subject to the output-size cap, G7).
    """

    peak_memory_bytes: int | None = None
    cpu_time_ms: int | None = None
    max_pids: int | None = None
    output_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Render to the stored jsonb (omitting ``None`` fields)."""
        raw: dict[str, object] = {}
        if self.peak_memory_bytes is not None:
            raw["peak_memory_bytes"] = self.peak_memory_bytes
        if self.cpu_time_ms is not None:
            raw["cpu_time_ms"] = self.cpu_time_ms
        if self.max_pids is not None:
            raw["max_pids"] = self.max_pids
        if self.output_bytes is not None:
            raw["output_bytes"] = self.output_bytes
        return raw

    @classmethod
    def from_dict(cls, raw: object) -> ResourceUsage | None:
        """Rebuild from a stored jsonb blob (``None`` / non-dict ⇒ ``None``)."""
        if not isinstance(raw, dict):
            return None

        def _int(key: str) -> int | None:
            value = raw.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return cls(
            peak_memory_bytes=_int("peak_memory_bytes"),
            cpu_time_ms=_int("cpu_time_ms"),
            max_pids=_int("max_pids"),
            output_bytes=_int("output_bytes"),
        )


@dataclass(frozen=True, slots=True)
class CodeRun:
    """One agent-authored sandbox code run — the ``code_runs`` row (ADR-0013 §4).

    Tenant- and owner-scoped (INV-1/INV-2): a run in another tenant or owned by
    another user is a 404 (existence non-disclosure). Records the exact ``code``
    executed (inspectable, E15-7/E6-5), the pinned ``image_digest`` actually used
    (reproducibility, E3-7), captured (output-size-capped, G7) ``stdout``/``stderr``,
    the ``exit_code``, ``duration_ms``, and measured ``resource_usage``. ``status``
    walks the :class:`CodeRunStatus` machine; a crash writes a terminal
    (``failed``), never a stuck ``running`` (ADR-0013 §5). ``artifact_ids`` are the
    output files the run emitted, collected via CC-B (#208) and tenant-scoped like
    any artifact. ``trace_id`` links the run to the parent agent trace (E15-7). The
    exact ``code`` + input refs + image digest are enough to re-run it.
    """

    id: UUID
    tenant_id: UUID
    owner_id: UUID
    status: CodeRunStatus
    code: str
    stdout: str
    stderr: str
    artifact_ids: list[UUID]
    created_at: datetime
    session_id: UUID | None = None
    run_id: UUID | None = None
    trace_id: UUID | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    resource_usage: ResourceUsage | None = None
    image_digest: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        """True once the run has reached a terminal status (never transitions again)."""
        return self.status in _TERMINAL_CODE_RUN_STATUSES
