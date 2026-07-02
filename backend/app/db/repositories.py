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
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.entities import (
    Artifact,
    ArtifactProducedBy,
    Assistant,
    AssistantStatus,
    AssistantVersion,
    AuditEvent,
    AuditOutcome,
    AutonomyLevel,
    Chunk,
    Citation,
    Collection,
    DigestCadence,
    Document,
    DocumentStatus,
    Grant,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    KnowledgeMode,
    KnowledgeScope,
    Message,
    MessageRole,
    OverlapPolicy,
    RecentSearch,
    RefreshToken,
    Role,
    Run,
    RunError,
    RunStatus,
    RunStep,
    RunStepKind,
    RunTrigger,
    SavedSearch,
    Schedule,
    ScheduleDelivery,
    Secret,
    SecretKind,
    Source,
    SourceStatus,
    Tenant,
    ToolInvocation,
    User,
    UserPreferences,
)
from app.domain.entities import ChatSession as ChatSessionEntity
from app.domain.scheduling import Cadence, StructuredCadence

# ---------------------------------------------------------------------------
# Row → domain mappers (the boundary: ORM rows never escape this module).
# ---------------------------------------------------------------------------


def _to_tenant(row: models.Tenant) -> Tenant:
    return Tenant(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        max_tool_turns=row.max_tool_turns,
    )


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


def _to_source(row: models.Source) -> Source:
    return Source(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        type=row.type,
        config=dict(row.config),
        status=SourceStatus(row.status),
        indexed_count=row.indexed_count,
        last_synced_at=row.last_synced_at,
        last_error=row.last_error,
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


def _to_artifact(row: models.Artifact) -> Artifact:
    return Artifact(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        produced_by=ArtifactProducedBy(row.produced_by),
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        sha256=row.sha256,
        session_id=row.session_id,
        run_id=row.run_id,
        tool_invocation_id=row.tool_invocation_id,
        retention_expires_at=row.retention_expires_at,
        created_at=row.created_at,
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
        assistant_id=row.assistant_id,
        assistant_version_id=row.assistant_version_id,
    )


def _to_user_preferences(row: models.UserPreference) -> UserPreferences:
    return UserPreferences(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        default_model=row.default_model,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_recent_search(row: models.RecentSearch) -> RecentSearch:
    return RecentSearch(query=row.query, last_used_at=row.last_used_at)


def _to_saved_search(row: models.SavedSearch) -> SavedSearch:
    return SavedSearch(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        name=row.name,
        query=row.query,
        collection_id=row.collection_id,
        source=row.source,
        type=row.type,
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


def _to_grant(row: models.Grant) -> Grant:
    return Grant(
        id=row.id,
        tenant_id=row.tenant_id,
        resource_type=GrantResourceType(row.resource_type),
        resource_id=row.resource_id,
        principal_type=GrantPrincipalType(row.principal_type),
        principal_id=row.principal_id,
        role=GrantRole(row.role),
        granted_by=row.granted_by,
        created_at=row.created_at,
    )


def _to_secret(row: models.Secret) -> Secret:
    return Secret(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        name=row.name,
        kind=SecretKind(row.kind),
        ciphertext=bytes(row.ciphertext),
        nonce=bytes(row.nonce),
        key_version=row.key_version,
        hint=row.hint,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


def _to_tool_invocation(row: models.ToolInvocation) -> ToolInvocation:
    return ToolInvocation(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        message_id=row.message_id,
        run_id=row.run_id,
        tool_name=row.tool_name,
        args_hash=row.args_hash,
        ok=row.ok,
        error=row.error,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
    )


def _to_knowledge_scope(raw: object) -> KnowledgeScope:
    """Map a stored ``knowledge_scope`` jsonb blob to the domain value object.

    Defensive against a partial/legacy row: a missing or malformed key degrades to
    the empty axis rather than raising, and an unrecognised mode is dropped — the
    stored scope can never make retrieval *widen* (a bad value narrows to nothing,
    never to "everything"). Ids are parsed as UUIDs; a non-uuid is skipped.
    """
    data = raw if isinstance(raw, dict) else {}

    def _uuids(key: str) -> tuple[UUID, ...]:
        values = data.get(key)
        if not isinstance(values, list):
            return ()
        out: list[UUID] = []
        for v in values:
            try:
                out.append(UUID(str(v)))
            except (ValueError, TypeError):
                continue
        return tuple(out)

    modes_raw = data.get("modes")
    modes: list[KnowledgeMode] = []
    if isinstance(modes_raw, list):
        for m in modes_raw:
            try:
                modes.append(KnowledgeMode(str(m)))
            except ValueError:
                continue
    return KnowledgeScope(
        collection_ids=_uuids("collection_ids"),
        source_ids=_uuids("source_ids"),
        modes=tuple(modes),
    )


def _knowledge_scope_to_json(scope: KnowledgeScope) -> dict[str, object]:
    """Serialise a :class:`KnowledgeScope` to the stored jsonb shape (uuids → str)."""
    return {
        "collection_ids": [str(c) for c in scope.collection_ids],
        "source_ids": [str(s) for s in scope.source_ids],
        "modes": [m.value for m in scope.modes],
    }


def _to_assistant(row: models.Assistant) -> Assistant:
    return Assistant(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        backup_owner_id=row.backup_owner_id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        model=row.model,
        knowledge_scope=_to_knowledge_scope(row.knowledge_scope),
        tool_allowlist=tuple(str(t) for t in (row.tool_allowlist or [])),
        autonomy_level=AutonomyLevel(row.autonomy_level),
        status=AssistantStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_assistant_version(row: models.AssistantVersion) -> AssistantVersion:
    return AssistantVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        assistant_id=row.assistant_id,
        version=row.version,
        author_id=row.author_id,
        config=dict(row.config or {}),
        notes=row.notes,
        diff_summary=row.diff_summary,
        created_at=row.created_at,
    )


def _to_run(row: models.Run) -> Run:
    return Run(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        assistant_id=row.assistant_id,
        assistant_version_id=row.assistant_version_id,
        schedule_id=row.schedule_id,
        session_id=row.session_id,
        trigger=RunTrigger(row.trigger),
        status=RunStatus(row.status),
        inputs=dict(row.inputs or {}),
        summary=row.summary,
        message_id=row.message_id,
        error=RunError.from_dict(row.error),
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


def _to_run_step(row: models.RunStep) -> RunStep:
    return RunStep(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        seq=row.seq,
        kind=RunStepKind(row.kind),
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


def _structured_from_json(raw: object) -> StructuredCadence | None:
    """Rebuild a :class:`StructuredCadence` from the stored jsonb blob, or ``None``."""
    if not isinstance(raw, dict):
        return None
    from app.domain.scheduling import CadenceUnit

    every = raw.get("every")
    at = raw.get("at")
    if not isinstance(every, str) or not isinstance(at, str):
        return None
    try:
        unit = CadenceUnit(every)
    except ValueError:
        return None
    dow = raw.get("day_of_week")
    dom = raw.get("day_of_month")
    return StructuredCadence(
        every=unit,
        at=at,
        day_of_week=dow if isinstance(dow, int) else None,
        day_of_month=dom if isinstance(dom, int) else None,
    )


def _cadence_from_row(row: models.Schedule) -> Cadence:
    """The row's canonical cadence: the stored cron + the original structured form."""
    structured = _structured_from_json(row.cadence_structured)
    return Cadence(cron=row.cadence_cron, structured=structured)


def _structured_to_json(sc: StructuredCadence) -> dict[str, object]:
    """Serialize a :class:`StructuredCadence` to a stored jsonb blob."""
    out: dict[str, object] = {"every": sc.every.value, "at": sc.at}
    if sc.day_of_week is not None:
        out["day_of_week"] = sc.day_of_week
    if sc.day_of_month is not None:
        out["day_of_month"] = sc.day_of_month
    return out


def _delivery_from_json(raw: object) -> ScheduleDelivery:
    """Rebuild a :class:`ScheduleDelivery` from the stored jsonb blob (fail-safe default)."""
    if not isinstance(raw, dict):
        return ScheduleDelivery.default()
    inbox = raw.get("inbox")
    digest_raw = raw.get("digest")
    digest: DigestCadence | None = None
    if isinstance(digest_raw, str):
        try:
            digest = DigestCadence(digest_raw)
        except ValueError:
            digest = None
    return ScheduleDelivery(
        inbox=bool(inbox) if isinstance(inbox, bool) else True,
        digest=digest,
    )


def _delivery_to_json(delivery: ScheduleDelivery) -> dict[str, object]:
    """Serialize a :class:`ScheduleDelivery` to a stored jsonb blob."""
    return {
        "inbox": delivery.inbox,
        "digest": delivery.digest.value if delivery.digest is not None else None,
    }


def _to_schedule(row: models.Schedule) -> Schedule:
    return Schedule(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        assistant_id=row.assistant_id,
        cadence=_cadence_from_row(row),
        timezone=row.timezone,
        input_params=dict(row.input_params or {}),
        delivery=_delivery_from_json(row.delivery),
        overlap_policy=OverlapPolicy(row.overlap_policy),
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_status=RunStatus(row.last_status) if row.last_status is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
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

    async def list_all(self) -> list[Tenant]:
        """Every tenant in stable order — for operator sweeps only.

        Powers cross-tenant maintenance like the search reindex backfill
        (ADR-0010 §5, ``python -m app.search.reindex``); never a request path
        (requests are always tenant-scoped).
        """
        stmt = select(models.Tenant).order_by(
            models.Tenant.created_at.asc(), models.Tenant.id.asc()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_tenant(r) for r in rows]

    async def update(self, tenant_id: UUID, *, max_tool_turns: int | None) -> Tenant | None:
        """Set the tenant's per-tenant chat tool-turn budget override (issue #148).

        ``max_tool_turns`` is written as given: an int sets the per-tenant
        override (the answer runtime caps its agentic loop at it), ``None`` clears
        it so the system default (``Settings.chat_max_tool_turns``) applies again.
        The DB ``ck_tenants_max_tool_turns_range`` check backstops the 1–50 band.
        Returns the updated entity, or ``None`` if no tenant with that id exists.
        """
        row = await self._session.get(models.Tenant, tenant_id)
        if row is None:
            return None
        row.max_tool_turns = max_tool_turns
        await self._session.flush()
        await self._session.refresh(row)
        return _to_tenant(row)


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


class UserPreferenceRepository(_TenantScopedRepository):
    """A user's account preferences within one tenant (spec 0005, epic #144).

    A per-user singleton keyed by ``(tenant_id, user_id)``. ``get`` returns
    ``None`` for a user who has never set a preference — the service maps that to
    the implicit server-default state **without** writing (read-before-write).
    ``set_default_model`` is the lazy upsert: it creates the row on first write,
    else updates it. Tenant-scoped (INV-1): a foreign-tenant/other-user row is
    invisible, so one user can never read or clobber another's preferences.
    """

    async def get(self, user_id: UUID) -> UserPreferences | None:
        stmt = select(models.UserPreference).where(
            models.UserPreference.tenant_id == self._tenant_id,
            models.UserPreference.user_id == user_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user_preferences(row) if row is not None else None

    async def set_default_model(self, user_id: UUID, default_model: str | None) -> UserPreferences:
        """Upsert the user's default-model override (``None`` clears it).

        Creates the row on first write (lazy), else updates the existing one, and
        returns the persisted state. Tenant-scoped (INV-1); the ``(tenant_id,
        user_id)`` singleton is the upsert target.
        """
        stmt = select(models.UserPreference).where(
            models.UserPreference.tenant_id == self._tenant_id,
            models.UserPreference.user_id == user_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = models.UserPreference(
                tenant_id=self._tenant_id,
                user_id=user_id,
                default_model=default_model,
            )
            self._session.add(row)
        else:
            row.default_model = default_model
        await self._session.flush()
        await self._session.refresh(row)
        return _to_user_preferences(row)


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


class SourceRepository(_TenantScopedRepository):
    """Connected sources within one tenant (ADR-0009 §4).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``source_id``
    resolves to ``None``/no rows, so the existence-non-disclosure 404 is enforced
    one layer up off the ``None`` return. Ownership is layered in
    ``services.sources_service`` (deny-by-default, spec 0004 §2.2).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        type: str,
        config: dict[str, object],
        status: SourceStatus = SourceStatus.PENDING,
    ) -> Source:
        row = models.Source(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            type=type,
            config=config,
            status=status.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_source(row)

    async def get(self, source_id: UUID) -> Source | None:
        stmt = select(models.Source).where(
            models.Source.tenant_id == self._tenant_id,
            models.Source.id == source_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_source(row) if row is not None else None

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[Source]:
        """A keyset page of an owner's sources (newest first).

        Owner- *and* tenant-scoped (deny-by-default ownership, spec 0004 §2.2 +
        INV-1): a caller only ever sees their own sources. Ordered by
        ``(created_at, id)`` **descending** with ``id`` the stable tiebreaker.
        ``after_id`` is the decoded cursor (previous page's last id); rows
        strictly after it are returned, capped at ``limit``. The boundary's
        ``created_at`` is resolved by a correlated scalar subquery so the
        comparison is exact on Postgres + the offline SQLite (mirrors the
        documents/collections keyset).
        """
        conditions = [
            models.Source.tenant_id == self._tenant_id,
            models.Source.owner_id == owner_id,
        ]
        if after_id is not None:
            boundary_created_at = (
                select(models.Source.created_at)
                .where(
                    models.Source.tenant_id == self._tenant_id,
                    models.Source.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Source.created_at < boundary_created_at,
                    and_(
                        models.Source.created_at == boundary_created_at,
                        models.Source.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Source)
            .where(*conditions)
            .order_by(models.Source.created_at.desc(), models.Source.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_source(r) for r in rows]

    async def update_status(
        self,
        source_id: UUID,
        *,
        status: SourceStatus,
        indexed_count: int | None = None,
        last_error: str | None = None,
        set_last_error: bool = False,
        last_synced_at: datetime | None = None,
        set_last_synced_at: bool = False,
    ) -> Source | None:
        """Advance a source's sync state (tenant-scoped, INV-1).

        Only supplied fields are touched. ``status`` is always set. ``indexed_count``
        is written when non-``None``. ``last_error``/``last_synced_at`` are
        tri-state: written (possibly to ``None`` to clear) only when the
        corresponding ``set_*`` flag is true. Returns the updated entity, or
        ``None`` if no row matches in this tenant. Ownership is enforced one layer
        up — this only guarantees tenancy.
        """
        stmt = select(models.Source).where(
            models.Source.tenant_id == self._tenant_id,
            models.Source.id == source_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        if indexed_count is not None:
            row.indexed_count = indexed_count
        if set_last_error:
            row.last_error = last_error
        if set_last_synced_at:
            row.last_synced_at = last_synced_at
        await self._session.flush()
        await self._session.refresh(row)
        return _to_source(row)

    async def delete(self, source_id: UUID) -> bool:
        """Delete the source **row** (tenant-scoped); ADR-0009 §5.

        Returns ``False`` when no row matches in this tenant (the service maps
        that to 404). Ownership is enforced one layer up. This removes only the
        ``sources`` row: the source's ingested documents (+ chunks), and the
        auto-created backing collection when it holds nothing else, are removed by
        :meth:`~app.services.sources_service.SourcesService.delete` *before* this
        call. They are **not** left to the ``documents.source_id`` FK ``ON DELETE
        CASCADE`` — an ORM parent delete nulls that nullable child FK before the
        DB cascade can fire (the #139 orphan bug), so the service deletes them
        explicitly. The FK cascade remains a DB-level backstop for non-ORM
        deletes.
        """
        stmt = select(models.Source).where(
            models.Source.tenant_id == self._tenant_id,
            models.Source.id == source_id,
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
        source_id: UUID | None = None,
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
            source_id=source_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_document(row)

    async def list_for_source(self, source_id: UUID) -> list[Document]:
        """List the documents a source ingested (tenant-scoped, INV-1).

        Used by the connector sync task to reconcile a re-sync: documents the
        previous sync produced for this source, so a re-fetch can replace rather
        than duplicate them. A foreign-tenant ``source_id`` returns no rows.
        """
        stmt = (
            select(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.source_id == source_id,
            )
            .order_by(models.Document.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document(r) for r in rows]

    async def get(self, document_id: UUID) -> Document | None:
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_document(row) if row is not None else None

    async def list_ids_page(self, *, after_id: UUID | None, limit: int) -> list[UUID]:
        """One keyset page of this tenant's document ids, ascending by id.

        The enumeration primitive for cross-corpus sweeps (the search reindex
        backfill, ADR-0010 §5): tenant-scoped (INV-1), ordered by ``id`` so a
        re-run resumes deterministically from the last id seen. ``after_id``
        is exclusive; ``None`` starts from the beginning.
        """
        stmt = select(models.Document.id).where(models.Document.tenant_id == self._tenant_id)
        if after_id is not None:
            stmt = stmt.where(models.Document.id > after_id)
        stmt = stmt.order_by(models.Document.id.asc()).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

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

    async def count_by_storage_key(self, storage_key: str) -> int:
        """Count this tenant's documents backed by ``storage_key``.

        Objects are content-addressed (``{tenant}/{sha256}/{filename}``), so two
        documents with identical bytes+filename share one object. Before deleting
        a stored object for a removed document, a caller checks this is ``0`` so
        it never deletes bytes another live document still references (INV-1:
        tenant-scoped, so a foreign tenant's identical bytes are a different key
        anyway).
        """
        stmt = (
            select(func.count())
            .select_from(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.storage_key == storage_key,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def delete(self, document_id: UUID) -> bool:
        """Delete a document (tenant-scoped); cascades to its chunks.

        Returns ``False`` when no row matches in this tenant (the service maps
        that to 404). Ownership is enforced one layer up — this only guarantees
        tenancy. The ORM ``delete-orphan`` cascade removes the document's chunks
        in the same transaction (INV-1: same-tenant chunks only). The backing
        object is NOT removed here — the caller owns object cleanup (the single-
        document path in ``DocumentService.delete``; the sync-reconcile path in
        ``app.tasks.sync_source``), because only it knows whether the content-
        addressed object is still referenced by another document.
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


class ArtifactRepository(_TenantScopedRepository):
    """Agent/run-produced artifacts within one tenant (issue #208).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``artifact_id``
    resolves to ``None``/no rows, so the existence-non-disclosure 404 is enforced
    one layer up (``services.artifacts_service``) off the ``None`` return.
    Ownership (deny-by-default, spec 0004 §2.2) is layered in the service.
    Artifacts are **immutable** — there is deliberately no ``update``/``set_*``
    method (a new version is a new row). Writes are flushed not committed (the
    caller owns the transaction boundary).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        produced_by: ArtifactProducedBy,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        sha256: str,
        session_id: UUID | None = None,
        run_id: UUID | None = None,
        tool_invocation_id: UUID | None = None,
        retention_expires_at: datetime | None = None,
    ) -> Artifact:
        row = models.Artifact(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            produced_by=produced_by.value,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            sha256=sha256,
            session_id=session_id,
            run_id=run_id,
            tool_invocation_id=tool_invocation_id,
            retention_expires_at=retention_expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_artifact(row)

    async def get(self, artifact_id: UUID) -> Artifact | None:
        stmt = select(models.Artifact).where(
            models.Artifact.tenant_id == self._tenant_id,
            models.Artifact.id == artifact_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_artifact(row) if row is not None else None

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        produced_by: ArtifactProducedBy | None = None,
        session_id: UUID | None = None,
    ) -> list[Artifact]:
        """A keyset page of an owner's artifacts (newest first), optionally filtered.

        Owner- *and* tenant-scoped (deny-by-default ownership, spec 0004 §2.2 +
        INV-1): a caller only ever sees their own artifacts. Ordered by
        ``(created_at, id)`` **descending** with ``id`` the stable tiebreaker, so
        the order is deterministic even when two rows share a timestamp.
        ``after_id`` is the decoded cursor (previous page's last id); rows strictly
        after it are returned, capped at ``limit``. The optional ``produced_by`` /
        ``session_id`` narrow the result. The boundary's ``created_at`` is resolved
        by a correlated scalar subquery so the comparison is exact on Postgres and
        on the offline SQLite (mirrors the documents keyset).
        """
        conditions = [
            models.Artifact.tenant_id == self._tenant_id,
            models.Artifact.owner_id == owner_id,
        ]
        if produced_by is not None:
            conditions.append(models.Artifact.produced_by == produced_by.value)
        if session_id is not None:
            conditions.append(models.Artifact.session_id == session_id)
        if after_id is not None:
            boundary_created_at = (
                select(models.Artifact.created_at)
                .where(
                    models.Artifact.tenant_id == self._tenant_id,
                    models.Artifact.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Artifact.created_at < boundary_created_at,
                    and_(
                        models.Artifact.created_at == boundary_created_at,
                        models.Artifact.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Artifact)
            .where(*conditions)
            .order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_artifact(r) for r in rows]

    async def delete(self, artifact_id: UUID) -> bool:
        """Delete an artifact **row** (tenant-scoped); returns ``False`` if absent.

        ``False`` when no row matches in this tenant (the service maps that to
        404). Ownership is enforced one layer up — this only guarantees tenancy.
        The stored object is removed by the service via the object store; this
        removes only the ``artifacts`` row.
        """
        stmt = select(models.Artifact).where(
            models.Artifact.tenant_id == self._tenant_id,
            models.Artifact.id == artifact_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def list_expired(self, *, now: datetime, limit: int = 100) -> list[Artifact]:
        """List artifacts whose retention window has elapsed (the janitor sweep, #208).

        Tenant-scoped (INV-1). Returns rows with a non-null ``retention_expires_at``
        strictly before ``now``, oldest-expiry first, capped at ``limit`` so the
        janitor purges in bounded batches. Rows with ``retention_expires_at IS
        NULL`` (keep forever) are never returned. The retention janitor
        (``app.tasks.artifact_retention``) drives this; it is a stub in this issue.
        """
        stmt = (
            select(models.Artifact)
            .where(
                models.Artifact.tenant_id == self._tenant_id,
                models.Artifact.retention_expires_at.is_not(None),
                models.Artifact.retention_expires_at < now,
            )
            .order_by(models.Artifact.retention_expires_at.asc(), models.Artifact.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_artifact(r) for r in rows]


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

    async def create(
        self,
        *,
        owner_id: UUID,
        model: str,
        title: str = "",
        assistant_id: UUID | None = None,
        assistant_version_id: UUID | None = None,
    ) -> ChatSessionEntity:
        row = models.ChatSession(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            model=model,
            title=title,
            assistant_id=assistant_id,
            assistant_version_id=assistant_version_id,
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


class SavedSearchRepository(_TenantScopedRepository):
    """Saved searches within one tenant (spec 0005, epic #144).

    Owner-scoped CRUD mirroring the chat-sessions keyset. Tenant-scoped (INV-1):
    a foreign-tenant/other-owner id resolves to ``None``/no rows, so the
    existence-non-disclosure 404 is enforced one layer up off the ``None`` return.
    Writes are flushed not committed (the caller owns the transaction boundary).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        name: str,
        query: str,
        collection_id: UUID | None = None,
        source: str | None = None,
        type: str | None = None,
    ) -> SavedSearch:
        row = models.SavedSearch(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            query=query,
            collection_id=collection_id,
            source=source,
            type=type,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_saved_search(row)

    async def get(self, saved_search_id: UUID) -> SavedSearch | None:
        stmt = select(models.SavedSearch).where(
            models.SavedSearch.tenant_id == self._tenant_id,
            models.SavedSearch.id == saved_search_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_saved_search(row) if row is not None else None

    async def list_for_owner_page(
        self, owner_id: UUID, *, limit: int, after_id: UUID | None = None
    ) -> list[SavedSearch]:
        """A keyset page of an owner's saved searches (newest-updated first).

        Owner- *and* tenant-scoped (spec 0004 §2.2 + INV-1). Ordered by
        ``(updated_at, id)`` **descending** with ``id`` the stable tiebreaker; the
        boundary's ``updated_at`` is resolved by a correlated scalar subquery
        (exact on Postgres + the offline SQLite), mirroring the chat-sessions keyset.
        """
        conditions = [
            models.SavedSearch.tenant_id == self._tenant_id,
            models.SavedSearch.owner_id == owner_id,
        ]
        if after_id is not None:
            boundary_updated_at = (
                select(models.SavedSearch.updated_at)
                .where(
                    models.SavedSearch.tenant_id == self._tenant_id,
                    models.SavedSearch.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.SavedSearch.updated_at < boundary_updated_at,
                    and_(
                        models.SavedSearch.updated_at == boundary_updated_at,
                        models.SavedSearch.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.SavedSearch)
            .where(*conditions)
            .order_by(models.SavedSearch.updated_at.desc(), models.SavedSearch.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_saved_search(r) for r in rows]

    async def update(
        self,
        saved_search_id: UUID,
        *,
        name: str | None = None,
        query: str | None = None,
        collection_id: UUID | None = None,
        set_collection_id: bool = False,
        source: str | None = None,
        set_source: bool = False,
        type: str | None = None,
        set_type: bool = False,
    ) -> SavedSearch | None:
        """Apply a partial update (tenant-scoped). Nullable filters are tri-state.

        ``name``/``query`` are written when non-``None``; ``collection_id`` /
        ``source`` / ``type`` are written (possibly to ``None`` to clear) only when
        their ``set_*`` flag is true. Returns ``None`` if no row matches in this
        tenant (the service maps that to 404); ownership is enforced one layer up.
        """
        stmt = select(models.SavedSearch).where(
            models.SavedSearch.tenant_id == self._tenant_id,
            models.SavedSearch.id == saved_search_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if query is not None:
            row.query = query
        if set_collection_id:
            row.collection_id = collection_id
        if set_source:
            row.source = source
        if set_type:
            row.type = type
        await self._session.flush()
        await self._session.refresh(row)
        return _to_saved_search(row)

    async def delete(self, saved_search_id: UUID) -> bool:
        stmt = select(models.SavedSearch).where(
            models.SavedSearch.tenant_id == self._tenant_id,
            models.SavedSearch.id == saved_search_id,
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


class GrantRepository(_TenantScopedRepository):
    """Explicit ACL grants within one tenant (spec 0004 §2.2, CC-1, INV-2).

    The persistence seam behind the widened retrieval permission filter: a grant
    row admits a (resource, principal) pair into the requester's allow-set. Every
    method is tenant-scoped (INV-1) — a grant minted in tenant A is invisible to a
    tenant-B repository, so a cross-tenant grant can never widen the filter
    (asserted by the negative tests). Ownership of the granted *resource* is
    enforced one layer up, in :class:`~app.services.grants_service.GrantsService`
    (only the owner/admin may grant). Like the other repositories, writes are
    flushed not committed — the caller owns the transaction boundary.
    """

    async def create(
        self,
        *,
        resource_type: GrantResourceType,
        resource_id: UUID,
        principal_type: GrantPrincipalType,
        principal_id: UUID,
        role: GrantRole,
        granted_by: UUID | None,
    ) -> Grant:
        """Persist a grant (idempotent on the unique (resource, principal) key).

        If a grant for the same ``(resource_type, resource_id, principal_type,
        principal_id)`` already exists in this tenant it is returned unchanged
        (re-granting is a no-op, never a duplicate-key error) — so a caller can
        safely "ensure" a grant. Otherwise a new row is inserted with this
        repository's tenant.
        """
        existing = await self._find(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        if existing is not None:
            return _to_grant(existing)
        row = models.Grant(
            tenant_id=self._tenant_id,
            resource_type=resource_type.value,
            resource_id=resource_id,
            principal_type=principal_type.value,
            principal_id=principal_id,
            role=role.value,
            granted_by=granted_by,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_grant(row)

    async def _find(
        self,
        *,
        resource_type: GrantResourceType,
        resource_id: UUID,
        principal_type: GrantPrincipalType,
        principal_id: UUID,
    ) -> models.Grant | None:
        """Locate the unique grant for a (resource, principal) pair, or ``None``."""
        stmt = select(models.Grant).where(
            models.Grant.tenant_id == self._tenant_id,
            models.Grant.resource_type == resource_type.value,
            models.Grant.resource_id == resource_id,
            models.Grant.principal_type == principal_type.value,
            models.Grant.principal_id == principal_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def granted_resource_ids(
        self, principal_id: UUID
    ) -> tuple[frozenset[UUID], frozenset[UUID]]:
        """The ``(document_ids, collection_ids)`` granted to a ``user`` principal.

        The resolved-id-set form of the SQL grant ``EXISTS`` (ADR-0010 §4): the
        retrieval chokepoint resolves the requester's grants per request and
        folds them into the engine's :class:`~app.search.filters.SearchAllowFilter`.
        Tenant-scoped (INV-1 — a cross-tenant grant never widens the filter) and
        ``user``-principal only (the MVP grant kind, spec 0004 §2.2). A revoked
        grant (row deleted) vanishes from the sets — deny-by-default restored.
        """
        stmt = select(models.Grant.resource_type, models.Grant.resource_id).where(
            models.Grant.tenant_id == self._tenant_id,
            models.Grant.principal_type == GrantPrincipalType.USER.value,
            models.Grant.principal_id == principal_id,
        )
        documents: set[UUID] = set()
        collections: set[UUID] = set()
        for resource_type, resource_id in (await self._session.execute(stmt)).all():
            if resource_type == GrantResourceType.DOCUMENT.value:
                documents.add(resource_id)
            elif resource_type == GrantResourceType.COLLECTION.value:
                collections.add(resource_id)
        return frozenset(documents), frozenset(collections)

    async def revoke(
        self,
        *,
        resource_type: GrantResourceType,
        resource_id: UUID,
        principal_type: GrantPrincipalType,
        principal_id: UUID,
    ) -> bool:
        """Delete a grant for a (resource, principal) pair (tenant-scoped).

        Returns ``True`` if a grant existed and was removed, ``False`` if none
        matched in this tenant (idempotent revoke). After this the retrieval
        filter no longer admits the resource for that principal — a revoked grant
        excludes the row again (the negative test asserts this).
        """
        row = await self._find(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def list_for_resource(
        self, *, resource_type: GrantResourceType, resource_id: UUID
    ) -> list[Grant]:
        """List the grants on one resource (tenant-scoped), oldest first."""
        stmt = (
            select(models.Grant)
            .where(
                models.Grant.tenant_id == self._tenant_id,
                models.Grant.resource_type == resource_type.value,
                models.Grant.resource_id == resource_id,
            )
            .order_by(models.Grant.created_at.asc(), models.Grant.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_grant(r) for r in rows]

    async def list_for_principal(
        self, *, principal_type: GrantPrincipalType, principal_id: UUID
    ) -> list[Grant]:
        """List the grants *to* one principal (tenant-scoped), oldest first.

        The read behind the retrieval filter's allow-set (the inverse of
        :meth:`list_for_resource`): every resource this principal has been granted
        within the tenant.
        """
        stmt = (
            select(models.Grant)
            .where(
                models.Grant.tenant_id == self._tenant_id,
                models.Grant.principal_type == principal_type.value,
                models.Grant.principal_id == principal_id,
            )
            .order_by(models.Grant.created_at.asc(), models.Grant.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_grant(r) for r in rows]


class SecretRepository(_TenantScopedRepository):
    """Encrypted per-tenant credentials within one tenant (issue #209, INV-1).

    The persistence seam for the secrets vault. Every method is tenant-scoped: a
    secret stored in tenant A is invisible to a tenant-B repository, so a
    cross-tenant id resolves to ``None`` (the 404 seam the service enforces).
    Rows only ever hold **ciphertext + nonce + key_version + hint** — the cipher
    (``app.core.crypto``) is applied one layer up in
    :class:`~app.services.secrets_service.SecretsService`, the sole caller. Reads
    return the full :class:`Secret` (ciphertext included) because the service needs
    the envelope to decrypt for an in-process adapter; the service never lets that
    ciphertext (or the plaintext) reach a router. Writes are flushed not committed —
    the caller owns the transaction boundary (audits atomically with the write).
    """

    async def get(self, secret_id: UUID) -> Secret | None:
        """Fetch one secret by id (tenant-scoped), or ``None``.

        Returns the full envelope (ciphertext/nonce/key_version) so the service can
        decrypt it internally; a foreign-tenant id returns ``None`` (INV-1), which
        the service maps to 404 (existence non-disclosure).
        """
        stmt = select(models.Secret).where(
            models.Secret.tenant_id == self._tenant_id,
            models.Secret.id == secret_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_secret(row) if row is not None else None

    async def get_by_owner_name(self, *, owner_id: UUID, name: str) -> Secret | None:
        """Fetch an owner's secret by its ``name`` handle (tenant-scoped), or ``None``.

        The stable lookup an in-process adapter uses ("the MCP-auth secret named
        ``…`` for this user"). Scoped to ``(tenant_id, owner_id, name)`` — the
        per-owner unique key.
        """
        stmt = select(models.Secret).where(
            models.Secret.tenant_id == self._tenant_id,
            models.Secret.owner_id == owner_id,
            models.Secret.name == name,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_secret(row) if row is not None else None

    async def upsert(
        self,
        *,
        owner_id: UUID,
        name: str,
        kind: SecretKind,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
        hint: str,
        created_by: UUID | None,
    ) -> Secret:
        """Store a secret, replacing an owner's existing one of the same ``name``.

        A secret name is a per-owner singleton (the ``UNIQUE(tenant_id, owner_id,
        name)``): storing the same name again **rotates the value** in place
        (new ciphertext/nonce/hint) rather than raising a duplicate-key error, so
        an adapter's handle stays stable across a credential change. The row is
        created in this repository's tenant with the given ``owner_id`` (both from
        the resolved principal, never request input — INV-1).
        """
        stmt = select(models.Secret).where(
            models.Secret.tenant_id == self._tenant_id,
            models.Secret.owner_id == owner_id,
            models.Secret.name == name,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = models.Secret(
                tenant_id=self._tenant_id,
                owner_id=owner_id,
                name=name,
                kind=kind.value,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=key_version,
                hint=hint,
                created_by=created_by,
            )
            self._session.add(row)
        else:
            row.kind = kind.value
            row.ciphertext = ciphertext
            row.nonce = nonce
            row.key_version = key_version
            row.hint = hint
        await self._session.flush()
        await self._session.refresh(row)
        return _to_secret(row)

    async def list_for_owner(self, owner_id: UUID) -> list[Secret]:
        """List an owner's secrets (tenant-scoped), oldest first.

        Returns full rows; the *service* projects them to the plaintext-free
        :class:`~app.domain.entities.SecretRef` before anything leaves — the
        repository does not decide the wire shape.
        """
        stmt = (
            select(models.Secret)
            .where(
                models.Secret.tenant_id == self._tenant_id,
                models.Secret.owner_id == owner_id,
            )
            .order_by(models.Secret.created_at.asc(), models.Secret.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_secret(r) for r in rows]

    async def delete(self, secret_id: UUID) -> bool:
        """Delete a secret by id (tenant-scoped); ``True`` if one was removed.

        Idempotent — ``False`` if no such secret exists in this tenant. Ownership
        is enforced one layer up in the service (owner-or-admin); a foreign-tenant
        id simply matches nothing here (INV-1).
        """
        stmt = select(models.Secret).where(
            models.Secret.tenant_id == self._tenant_id,
            models.Secret.id == secret_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


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


class ToolInvocationRepository(_TenantScopedRepository):
    """The ``tool_invocations`` trace within one tenant (CC-7 / issue #207 §4).

    One ``record`` per governed tool invocation — a success, an
    off-allow-list/unapproved **denial**, or a tool **failure** — so the per-run
    trace never has a silent gap. Tenant-scoped (INV-1): a call in tenant A is
    invisible to a tenant-B repository. This is a trace/analytics table, not the
    append-only audit log (that stays :class:`AuditEventRepository`), so it is an
    ordinary table (no UPDATE/DELETE revoke). Writes are flushed not committed —
    the caller (the runner, within the chat runtime's transaction) owns the commit
    so the invocation row lands atomically with the answer turn it belongs to.
    """

    async def record(
        self,
        *,
        tool_name: str,
        args_hash: str,
        ok: bool,
        duration_ms: int,
        session_id: UUID | None = None,
        message_id: UUID | None = None,
        run_id: UUID | None = None,
        error: str | None = None,
    ) -> ToolInvocation:
        """Append one tool-invocation record for this tenant, returning it."""
        row = models.ToolInvocation(
            tenant_id=self._tenant_id,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            tool_name=tool_name,
            args_hash=args_hash,
            ok=ok,
            error=error,
            duration_ms=max(0, duration_ms),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_tool_invocation(row)

    async def list_for_session(self, session_id: UUID, *, limit: int = 200) -> list[ToolInvocation]:
        """The tool trace for one chat session (tenant-scoped), oldest first."""
        stmt = (
            select(models.ToolInvocation)
            .where(
                models.ToolInvocation.tenant_id == self._tenant_id,
                models.ToolInvocation.session_id == session_id,
            )
            .order_by(models.ToolInvocation.created_at.asc(), models.ToolInvocation.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_tool_invocation(r) for r in rows]


class AssistantRepository(_TenantScopedRepository):
    """Assistants (the mutable head) within one tenant (ADR-0011 §1, #211).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``assistant_id``
    resolves to ``None``/no rows, so the existence-non-disclosure 404 is enforced
    one layer up off the ``None`` return. Ownership/grant visibility is layered in
    ``services.assistants_service`` (deny-by-default, spec 0004 §2.2). Persists via
    the session but does not commit — the caller owns the transaction boundary.
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        knowledge_scope: KnowledgeScope | None = None,
        tool_allowlist: Sequence[str] = (),
        autonomy_level: AutonomyLevel = AutonomyLevel.SUGGEST,
        backup_owner_id: UUID | None = None,
    ) -> Assistant:
        """Create a ``draft`` assistant owned by ``owner_id`` (ADR-0011 §1)."""
        row = models.Assistant(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            backup_owner_id=backup_owner_id,
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            knowledge_scope=_knowledge_scope_to_json(knowledge_scope or KnowledgeScope.empty()),
            tool_allowlist=list(tool_allowlist),
            autonomy_level=autonomy_level.value,
            status=AssistantStatus.DRAFT.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_assistant(row)

    async def get(self, assistant_id: UUID) -> Assistant | None:
        stmt = select(models.Assistant).where(
            models.Assistant.tenant_id == self._tenant_id,
            models.Assistant.id == assistant_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_assistant(row) if row is not None else None

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        status: AssistantStatus | None = None,
    ) -> list[Assistant]:
        """A keyset page of an owner's assistants (newest first).

        Owner- *and* tenant-scoped (deny-by-default ownership, spec 0004 §2.2 +
        INV-1): a caller only ever sees their own here (sharing via grants is a
        follow-up, ADR-0011 §1). Ordered by ``(created_at, id)`` **descending**
        with ``id`` the stable tiebreaker; ``after_id`` is the decoded cursor and
        the boundary ``created_at`` is resolved by a correlated scalar subquery so
        the comparison is exact on Postgres + the offline SQLite (mirrors the
        collections/sources keyset). An optional ``status`` filters the page.
        """
        conditions = [
            models.Assistant.tenant_id == self._tenant_id,
            models.Assistant.owner_id == owner_id,
        ]
        if status is not None:
            conditions.append(models.Assistant.status == status.value)
        if after_id is not None:
            boundary_created_at = (
                select(models.Assistant.created_at)
                .where(
                    models.Assistant.tenant_id == self._tenant_id,
                    models.Assistant.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Assistant.created_at < boundary_created_at,
                    and_(
                        models.Assistant.created_at == boundary_created_at,
                        models.Assistant.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Assistant)
            .where(*conditions)
            .order_by(models.Assistant.created_at.desc(), models.Assistant.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_assistant(r) for r in rows]

    async def update(
        self,
        assistant_id: UUID,
        *,
        fields: dict[str, object],
    ) -> Assistant | None:
        """Apply a partial update to the mutable head, tenant-scoped.

        ``fields`` carries only the attributes to change (already validated by the
        service). ``knowledge_scope`` is expected as a :class:`KnowledgeScope` and
        is serialised here; ``autonomy_level`` as an :class:`AutonomyLevel`;
        ``status`` as an :class:`AssistantStatus`; ``tool_allowlist`` as a
        sequence of strings. Returns the updated entity, or ``None`` if no row
        matches in this tenant (the service maps that to 404). Ownership is
        enforced one layer up.
        """
        stmt = select(models.Assistant).where(
            models.Assistant.tenant_id == self._tenant_id,
            models.Assistant.id == assistant_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        for key, value in fields.items():
            if key == "knowledge_scope" and isinstance(value, KnowledgeScope):
                row.knowledge_scope = _knowledge_scope_to_json(value)
            elif key == "tool_allowlist" and isinstance(value, list | tuple):
                row.tool_allowlist = [str(t) for t in value]
            elif key == "autonomy_level" and isinstance(value, AutonomyLevel):
                row.autonomy_level = value.value
            elif key == "status" and isinstance(value, AssistantStatus):
                row.status = value.value
            else:
                setattr(row, key, value)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_assistant(row)

    async def delete(self, assistant_id: UUID) -> bool:
        """Delete an assistant head (cascades to its version history), tenant-scoped."""
        stmt = select(models.Assistant).where(
            models.Assistant.tenant_id == self._tenant_id,
            models.Assistant.id == assistant_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        return True


class AssistantVersionRepository(_TenantScopedRepository):
    """The immutable, append-only ``assistant_versions`` history within one tenant.

    Exposes only ``add`` (append), reads, and the monotonic ``next_version``
    helper — there is intentionally no update or delete method; the table also
    denies UPDATE/DELETE at the DB role (the ``0016`` migration). A rollback is an
    ``add`` of a **new** version copying a prior config — history is never
    rewritten. Tenant-scoped (INV-1).
    """

    async def add(
        self,
        *,
        assistant_id: UUID,
        version: int,
        author_id: UUID | None,
        config: dict[str, object],
        notes: str | None = None,
        diff_summary: str | None = None,
    ) -> AssistantVersion:
        """Append one immutable version snapshot for an assistant, returning it."""
        row = models.AssistantVersion(
            tenant_id=self._tenant_id,
            assistant_id=assistant_id,
            version=version,
            author_id=author_id,
            config=config,
            notes=notes,
            diff_summary=diff_summary,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_assistant_version(row)

    async def next_version(self, assistant_id: UUID) -> int:
        """The next monotonic version number for an assistant (``max+1``, min 1)."""
        stmt = select(func.max(models.AssistantVersion.version)).where(
            models.AssistantVersion.tenant_id == self._tenant_id,
            models.AssistantVersion.assistant_id == assistant_id,
        )
        current = (await self._session.execute(stmt)).scalar_one_or_none()
        return int(current) + 1 if current is not None else 1

    async def get(self, version_id: UUID) -> AssistantVersion | None:
        stmt = select(models.AssistantVersion).where(
            models.AssistantVersion.tenant_id == self._tenant_id,
            models.AssistantVersion.id == version_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_assistant_version(row) if row is not None else None

    async def get_by_number(
        self, assistant_id: UUID, version: int
    ) -> AssistantVersion | None:
        """The specific numbered version of an assistant (for rollback), tenant-scoped."""
        stmt = select(models.AssistantVersion).where(
            models.AssistantVersion.tenant_id == self._tenant_id,
            models.AssistantVersion.assistant_id == assistant_id,
            models.AssistantVersion.version == version,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_assistant_version(row) if row is not None else None

    async def get_head(self, assistant_id: UUID) -> AssistantVersion | None:
        """The current (highest-numbered) published version, or ``None`` if never published."""
        stmt = (
            select(models.AssistantVersion)
            .where(
                models.AssistantVersion.tenant_id == self._tenant_id,
                models.AssistantVersion.assistant_id == assistant_id,
            )
            .order_by(models.AssistantVersion.version.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_assistant_version(row) if row is not None else None

    async def list_for_assistant_page(
        self,
        assistant_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[AssistantVersion]:
        """A keyset page of an assistant's versions (newest version first), tenant-scoped.

        Ordered by ``version`` **descending** (a total order per assistant — the
        UNIQUE guarantees no ties), so the id-only cursor resolves the boundary's
        version by a correlated scalar subquery (exact on Postgres + SQLite).
        """
        conditions = [
            models.AssistantVersion.tenant_id == self._tenant_id,
            models.AssistantVersion.assistant_id == assistant_id,
        ]
        if after_id is not None:
            boundary_version = (
                select(models.AssistantVersion.version)
                .where(
                    models.AssistantVersion.tenant_id == self._tenant_id,
                    models.AssistantVersion.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(models.AssistantVersion.version < boundary_version)
        stmt = (
            select(models.AssistantVersion)
            .where(*conditions)
            .order_by(models.AssistantVersion.version.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_assistant_version(r) for r in rows]


class RunRepository(_TenantScopedRepository):
    """Headless agent-run records within one tenant (ADR-0015 §2, #235).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``run_id``
    resolves to ``None`` (the existence-non-disclosure 404 is enforced one layer
    up in ``services.runs_service`` off the ``None`` return). Owner visibility is
    layered in the service (deny-by-default, spec 0004 §2.2). Persists via the
    session but does not commit — the caller owns the transaction boundary.

    The run's ``status`` is written by the runtime as it walks the state machine
    (``queued`` → ``running`` → a terminal); a crash-safe task always writes a
    terminal, never leaving a stuck ``running`` (ADR-0015 §5, INV-8).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        assistant_id: UUID,
        assistant_version_id: UUID | None,
        trigger: RunTrigger,
        inputs: dict[str, object] | None = None,
        schedule_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> Run:
        """Create a ``queued`` run for an assistant (ADR-0015 §4 — fire/run-now enqueue).

        ``run_id`` may be pre-minted so it doubles as the WS ``streamId`` known to
        the enqueuer before the task starts (ADR-0015 §3); omitted ⇒ generated.
        """
        row = models.Run(
            id=run_id or uuid4(),
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            assistant_id=assistant_id,
            assistant_version_id=assistant_version_id,
            schedule_id=schedule_id,
            trigger=trigger.value,
            status=RunStatus.QUEUED.value,
            inputs=dict(inputs or {}),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_run(row)

    async def get(self, run_id: UUID) -> Run | None:
        stmt = select(models.Run).where(
            models.Run.tenant_id == self._tenant_id,
            models.Run.id == run_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_run(row) if row is not None else None

    async def count_active_for_schedule(self, schedule_id: UUID) -> int:
        """Count a schedule's still-active runs (``queued``/``running``) — the overlap gate.

        Tenant-scoped (INV-1). The dispatcher's overlap check (ADR-0015 §5) keys on
        this before enqueuing so a slow scheduled run never stacks up under the
        default ``skip`` policy. Zero for a schedule with no active run.
        """
        stmt = (
            select(func.count())
            .select_from(models.Run)
            .where(
                models.Run.tenant_id == self._tenant_id,
                models.Run.schedule_id == schedule_id,
                models.Run.status.in_(
                    [RunStatus.QUEUED.value, RunStatus.RUNNING.value]
                ),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def mark_running(self, run_id: UUID, *, started_at: datetime) -> Run | None:
        """Transition a run to ``running`` and stamp ``started_at``, tenant-scoped."""
        row = await self._get_row(run_id)
        if row is None:
            return None
        row.status = RunStatus.RUNNING.value
        row.started_at = started_at
        await self._session.flush()
        return _to_run(row)

    async def mark_terminal(
        self,
        run_id: UUID,
        *,
        status: RunStatus,
        finished_at: datetime,
        summary: str | None = None,
        message_id: UUID | None = None,
        session_id: UUID | None = None,
        error: RunError | None = None,
    ) -> Run | None:
        """Write a run's terminal status + outputs (ADR-0015 §5), tenant-scoped.

        The single terminal-writing method: sets one of ``succeeded``/``failed``/
        ``escalated``, ``finished_at``, and any produced ``summary``/``message_id``/
        ``session_id``/``error``. A crash path calls this with ``failed`` + a typed
        error so a run never ends in silence (INV-8, never a stuck ``running``).
        """
        row = await self._get_row(run_id)
        if row is None:
            return None
        row.status = status.value
        row.finished_at = finished_at
        if summary is not None:
            row.summary = summary
        if message_id is not None:
            row.message_id = message_id
        if session_id is not None:
            row.session_id = session_id
        row.error = error.to_dict() if error is not None else None
        await self._session.flush()
        return _to_run(row)

    async def _get_row(self, run_id: UUID) -> models.Run | None:
        stmt = select(models.Run).where(
            models.Run.tenant_id == self._tenant_id,
            models.Run.id == run_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        assistant_id: UUID | None = None,
        schedule_id: UUID | None = None,
        status: RunStatus | None = None,
    ) -> list[Run]:
        """A keyset page of an owner's runs (newest first) — the ``/runs`` inbox.

        Owner- *and* tenant-scoped (spec 0004 §2.2 + INV-1): a caller only ever
        sees their own runs. Optional ``assistant_id`` / ``schedule_id`` / ``status``
        filters compose (the frozen ``GET /runs`` query params). Ordered by
        ``(created_at, id)`` **descending** with ``id`` the stable tiebreaker; the
        id-only cursor resolves the boundary ``created_at`` by a correlated scalar
        subquery (exact on Postgres + the offline SQLite), mirroring the assistants
        keyset.
        """
        conditions = [
            models.Run.tenant_id == self._tenant_id,
            models.Run.owner_id == owner_id,
        ]
        if assistant_id is not None:
            conditions.append(models.Run.assistant_id == assistant_id)
        if schedule_id is not None:
            conditions.append(models.Run.schedule_id == schedule_id)
        if status is not None:
            conditions.append(models.Run.status == status.value)
        if after_id is not None:
            boundary_created_at = (
                select(models.Run.created_at)
                .where(
                    models.Run.tenant_id == self._tenant_id,
                    models.Run.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Run.created_at < boundary_created_at,
                    and_(
                        models.Run.created_at == boundary_created_at,
                        models.Run.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Run)
            .where(*conditions)
            .order_by(models.Run.created_at.desc(), models.Run.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_run(r) for r in rows]


class RunStepRepository(_TenantScopedRepository):
    """The append-only run-transcript (``run_steps``) within one tenant (#235).

    Exposes ``add`` (append one envelope) + reads — there is intentionally no
    update or delete: a transcript step is written once (the ``RunTranscriptSink``
    appends per published envelope) and read back on the run detail. ``(run_id,
    seq)`` is UNIQUE (the migration) so a step can never be duplicated or reordered.
    Tenant-scoped (INV-1).
    """

    async def add(
        self,
        *,
        run_id: UUID,
        seq: int,
        kind: RunStepKind,
        payload: dict[str, object],
    ) -> RunStep:
        """Append one transcript step (the durable analogue of a WS envelope)."""
        row = models.RunStep(
            tenant_id=self._tenant_id,
            run_id=run_id,
            seq=seq,
            kind=kind.value,
            payload=dict(payload),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_run_step(row)

    async def list_for_run(self, run_id: UUID) -> list[RunStep]:
        """The full ordered transcript of a run (by ``seq`` ascending), tenant-scoped."""
        stmt = (
            select(models.RunStep)
            .where(
                models.RunStep.tenant_id == self._tenant_id,
                models.RunStep.run_id == run_id,
            )
            .order_by(models.RunStep.seq.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_run_step(r) for r in rows]


class ScheduleRepository(_TenantScopedRepository):
    """Recurring-run definitions (``schedules``) within one tenant (ADR-0015 §2, #236).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``schedule_id``
    resolves to ``None``, so the existence-non-disclosure 404 is enforced one layer
    up in ``services.schedules_service``. Owner visibility (deny-by-default, spec
    0004 §2.2) is layered in the service. Persists via the session but does not
    commit — the caller owns the transaction boundary.

    The cadence is stored as its normalized canonical cron (``cadence_cron``) plus
    the optional original structured form (``cadence_structured``); the service is
    the only thing that normalizes/validates a cadence (it owns the domain
    ``scheduling`` helpers), so the repository takes the already-normalized
    :class:`Cadence`.
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        assistant_id: UUID,
        cadence: Cadence,
        timezone: str,
        input_params: dict[str, object] | None = None,
        delivery: ScheduleDelivery | None = None,
        overlap_policy: OverlapPolicy = OverlapPolicy.SKIP,
        enabled: bool = True,
        next_run_at: datetime | None = None,
    ) -> Schedule:
        """Create a schedule owned by ``owner_id`` (ADR-0015 §2)."""
        row = models.Schedule(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            assistant_id=assistant_id,
            cadence_cron=cadence.cron,
            cadence_structured=(
                _structured_to_json(cadence.structured) if cadence.structured is not None else None
            ),
            timezone=timezone,
            input_params=dict(input_params or {}),
            delivery=_delivery_to_json(delivery or ScheduleDelivery.default()),
            overlap_policy=overlap_policy.value,
            enabled=enabled,
            next_run_at=next_run_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_schedule(row)

    async def get(self, schedule_id: UUID) -> Schedule | None:
        row = await self._get_row(schedule_id)
        return _to_schedule(row) if row is not None else None

    async def _get_row(self, schedule_id: UUID) -> models.Schedule | None:
        stmt = select(models.Schedule).where(
            models.Schedule.tenant_id == self._tenant_id,
            models.Schedule.id == schedule_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(
        self,
        schedule_id: UUID,
        *,
        cadence: Cadence | None = None,
        timezone: str | None = None,
        input_params: dict[str, object] | None = None,
        delivery: ScheduleDelivery | None = None,
        overlap_policy: OverlapPolicy | None = None,
        enabled: bool | None = None,
        next_run_at: datetime | None = None,
        clear_next_run_at: bool = False,
    ) -> Schedule | None:
        """Apply a partial update to a schedule, tenant-scoped.

        Only non-``None`` arguments are written (the service already validated
        them). ``next_run_at`` is set when supplied; ``clear_next_run_at`` explicitly
        nulls it (pause), distinguishing "not touched" from "cleared". Returns the
        updated entity, or ``None`` if no row matches in this tenant.
        """
        row = await self._get_row(schedule_id)
        if row is None:
            return None
        if cadence is not None:
            row.cadence_cron = cadence.cron
            row.cadence_structured = (
                _structured_to_json(cadence.structured) if cadence.structured is not None else None
            )
        if timezone is not None:
            row.timezone = timezone
        if input_params is not None:
            row.input_params = dict(input_params)
        if delivery is not None:
            row.delivery = _delivery_to_json(delivery)
        if overlap_policy is not None:
            row.overlap_policy = overlap_policy.value
        if enabled is not None:
            row.enabled = enabled
        if clear_next_run_at:
            row.next_run_at = None
        elif next_run_at is not None:
            row.next_run_at = next_run_at
        await self._session.flush()
        # Refresh so the ``onupdate`` server-side ``updated_at`` is materialized before
        # mapping (a mutating flush expires it; reading it in ``_to_schedule`` otherwise
        # triggers a lazy DB load in a sync context).
        await self._session.refresh(row)
        return _to_schedule(row)

    async def record_fire(
        self,
        schedule_id: UUID,
        *,
        last_run_at: datetime,
        last_status: RunStatus,
        next_run_at: datetime | None,
    ) -> Schedule | None:
        """Stamp a fire's summary (last_run_at/last_status) + the recomputed next fire.

        Called by the dispatcher after a real fire enqueues a run (ADR-0015 §4):
        records when the schedule last fired and with what run status, and advances
        ``next_run_at`` to the next tz/DST-correct instant. Tenant-scoped.
        """
        row = await self._get_row(schedule_id)
        if row is None:
            return None
        row.last_run_at = last_run_at
        row.last_status = last_status.value
        row.next_run_at = next_run_at
        await self._session.flush()
        await self._session.refresh(row)
        return _to_schedule(row)

    async def advance_next_run(
        self, schedule_id: UUID, *, next_run_at: datetime | None
    ) -> Schedule | None:
        """Advance ``next_run_at`` **without** touching last_run_at/last_status, tenant-scoped.

        Used when a fire is *skipped* or *deferred* (overlap/concurrency/rate,
        ADR-0015 §5): the schedule keeps ticking to its next fire, but a skipped fire
        is not a run — the prior run's ``last_status`` stands, and no fake fire time
        is recorded.
        """
        row = await self._get_row(schedule_id)
        if row is None:
            return None
        row.next_run_at = next_run_at
        await self._session.flush()
        await self._session.refresh(row)
        return _to_schedule(row)

    async def delete(self, schedule_id: UUID) -> bool:
        """Delete a schedule, tenant-scoped. Returns whether a row was removed."""
        row = await self._get_row(schedule_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        assistant_id: UUID | None = None,
        enabled: bool | None = None,
    ) -> list[Schedule]:
        """A keyset page of an owner's schedules (newest first) — the ``/schedules`` list.

        Owner- *and* tenant-scoped (spec 0004 §2.2 + INV-1). Optional
        ``assistant_id`` / ``enabled`` filters compose (the frozen ``GET /schedules``
        query params). Ordered by ``(created_at, id)`` descending; the id-only cursor
        resolves the boundary ``created_at`` by a correlated scalar subquery (exact on
        Postgres + the offline SQLite), mirroring the runs keyset.
        """
        conditions = [
            models.Schedule.tenant_id == self._tenant_id,
            models.Schedule.owner_id == owner_id,
        ]
        if assistant_id is not None:
            conditions.append(models.Schedule.assistant_id == assistant_id)
        if enabled is not None:
            conditions.append(models.Schedule.enabled == enabled)
        if after_id is not None:
            boundary_created_at = (
                select(models.Schedule.created_at)
                .where(
                    models.Schedule.tenant_id == self._tenant_id,
                    models.Schedule.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.Schedule.created_at < boundary_created_at,
                    and_(
                        models.Schedule.created_at == boundary_created_at,
                        models.Schedule.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.Schedule)
            .where(*conditions)
            .order_by(models.Schedule.created_at.desc(), models.Schedule.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_schedule(r) for r in rows]


class ScheduleReconcileRepository:
    """Cross-tenant read of enabled schedules — for the Beat reconcile sweep ONLY (ADR-0015 §1).

    **Not** tenant-scoped (it is the one schedule read that spans tenants), mirroring
    :class:`TenantRepository` / the search reindex sweep: the dynamic Beat rebuilds
    its derived RedBeat entries from Postgres (the source of truth) on boot / on a
    lost Redis entry, so it must enumerate every tenant's enabled schedules. It runs
    under a **bypass**-scoped session (``bind_bypass``) — a deliberate, system-only
    path, never a request path (requests are always tenant-scoped, INV-1). Read-only.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_enabled(self) -> list[Schedule]:
        """Every enabled schedule across all tenants, oldest first (the reconcile set)."""
        stmt = (
            select(models.Schedule)
            .where(models.Schedule.enabled.is_(True))
            .order_by(models.Schedule.created_at.asc(), models.Schedule.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_schedule(r) for r in rows]


def normalize_query(query: str) -> str:
    """Dedupe key for a recent search — trimmed, lower-cased, whitespace-collapsed.

    So ``"  Acme   Renewal "`` and ``"acme renewal"`` are the same recent entry.
    Returns ``""`` for a blank query (the caller skips recording it).
    """
    return " ".join(query.strip().lower().split())


class RecentSearchRepository(_TenantScopedRepository):
    """A user's recent ``/search`` history within one tenant (spec 0005, epic #144).

    De-duplicated by normalized query (re-running a query bumps one row's
    ``last_used_at`` rather than inserting a duplicate) and capped per user (oldest
    evicted). Tenant-scoped (INV-1): a foreign-tenant/other-user row is invisible,
    so one user's recents never surface for another. Writes are flushed not
    committed (the caller owns the transaction).
    """

    async def record(self, user_id: UUID, query: str, *, cap: int = 20) -> None:
        """Record (or bump) a query in the user's recent history (deduped, capped).

        A blank query is ignored. An **atomic upsert** (``INSERT … ON CONFLICT
        (tenant_id, user_id, normalized_query) DO UPDATE``) rather than
        select-then-insert, so two concurrent identical ``/search`` requests can't
        race into a unique-violation that would fail the search — one inserts, the
        other updates ``last_used_at`` (recent history is only a side effect, it
        must never break a search). The newest ``cap`` distinct queries are kept;
        older ones are evicted so the list stays bounded per user.
        """
        normalized = normalize_query(query)
        if not normalized:
            return
        now = datetime.now(UTC)
        display = query.strip()
        values = {
            "id": uuid4(),
            "tenant_id": self._tenant_id,
            "user_id": user_id,
            "query": display,
            "normalized_query": normalized,
            "last_used_at": now,
        }
        conflict = ["tenant_id", "user_id", "normalized_query"]
        update = {"query": display, "last_used_at": now}
        # Dialect-aware upsert: Postgres + the offline SQLite both support
        # ON CONFLICT DO UPDATE on the unique (tenant, user, normalized) target.
        # Executed per-branch (the two dialect Insert types don't share a variable).
        dialect = self._session.bind.dialect.name if self._session.bind is not None else ""
        if dialect == "postgresql":
            await self._session.execute(
                pg_insert(models.RecentSearch)
                .values(**values)
                .on_conflict_do_update(index_elements=conflict, set_=update)
            )
        else:
            await self._session.execute(
                sqlite_insert(models.RecentSearch)
                .values(**values)
                .on_conflict_do_update(index_elements=conflict, set_=update)
            )
        await self._session.flush()
        await self._evict_beyond_cap(user_id, cap)

    async def _evict_beyond_cap(self, user_id: UUID, cap: int) -> None:
        """Delete the oldest recents beyond ``cap`` for this user (tenant-scoped)."""
        stmt = (
            select(models.RecentSearch)
            .where(
                models.RecentSearch.tenant_id == self._tenant_id,
                models.RecentSearch.user_id == user_id,
            )
            .order_by(models.RecentSearch.last_used_at.asc(), models.RecentSearch.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        excess = len(rows) - cap
        for row in rows[: max(0, excess)]:
            await self._session.delete(row)
        if excess > 0:
            await self._session.flush()

    async def list_for_user(self, user_id: UUID, *, limit: int) -> list[RecentSearch]:
        """The user's recent queries, newest-used first (capped by ``limit``)."""
        stmt = (
            select(models.RecentSearch)
            .where(
                models.RecentSearch.tenant_id == self._tenant_id,
                models.RecentSearch.user_id == user_id,
            )
            .order_by(models.RecentSearch.last_used_at.desc(), models.RecentSearch.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_recent_search(r) for r in rows]

    async def clear_for_user(self, user_id: UUID) -> None:
        """Clear all of the user's recent searches (tenant-scoped; idempotent)."""
        stmt = select(models.RecentSearch).where(
            models.RecentSearch.tenant_id == self._tenant_id,
            models.RecentSearch.user_id == user_id,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
