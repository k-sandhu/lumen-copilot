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

import ipaddress
import uuid as uuid_mod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql import insert as pg_upsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db import models
from app.domain.audit import AuditSourceOrigin
from app.domain.chat import AskUserQuestion
from app.domain.entities import (
    Artifact,
    ArtifactProducedBy,
    Assistant,
    AssistantStatus,
    AssistantVersion,
    AuditEvent,
    AuditOutcome,
    AutonomyLevel,
    CertificationState,
    Chunk,
    Citation,
    CodeRun,
    CodeRunStatus,
    Collection,
    DigestCadence,
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentUpload,
    DocumentUploadState,
    Grant,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Group,
    GroupKind,
    KnowledgeMode,
    KnowledgeScope,
    LlmProvider,
    LlmProviderStatus,
    LlmUsageRecord,
    LlmUsageTotals,
    McpServer,
    McpServerStatus,
    Message,
    MessageRole,
    OverlapPolicy,
    RecentSearch,
    RefreshToken,
    ResourceUsage,
    Role,
    Run,
    RunDelivery,
    RunDeliveryKind,
    RunDeliveryStatus,
    RunError,
    RunStatus,
    RunStep,
    RunStepKind,
    RunTrigger,
    SandboxSession,
    SandboxSessionStatus,
    SavedSearch,
    Schedule,
    ScheduleDelivery,
    Secret,
    SecretKind,
    SessionSummary,
    Source,
    SourceStatus,
    Tenant,
    TenantAutonomyPolicy,
    TenantSandboxPolicy,
    TenantToolPolicy,
    ToolInvocation,
    TranscriptionCheckpoint,
    TranscriptSegment,
    TranscriptSpeaker,
    User,
    UserPreferences,
)
from app.domain.entities import ChatSession as ChatSessionEntity
from app.domain.scheduling import Cadence, StructuredCadence


class _Unset:
    """Sentinel for "field omitted" in a tri-state partial update.

    Distinguishes "leave unchanged" (``_UNSET``) from "set to ``None``" (clear) so a
    single upsert can update any subset of a row's nullable fields without a bespoke
    method per field.
    """


_UNSET = _Unset()

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
        logo_key=row.logo_key,
        # Container-shape guard (#440 NEW-4): only a JSON LIST is config; a
        # scalar/object smuggled into the column must neither crash tenant
        # loading (500ing every answer) nor be reinterpreted as candidates.
        fallback_models=(
            list(row.fallback_models)
            if isinstance(row.fallback_models, list) and row.fallback_models
            else None
        ),
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
        avatar_key=row.avatar_key,
        email_attested_at=row.email_attested_at,
        email_attested_by=row.email_attested_by,
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
        auth_secret_ref=row.auth_secret_ref,
        connect_generation=row.connect_generation,
        connected_account=dict(row.connected_account)
        if row.connected_account is not None
        else None,
        sync_cursor=row.sync_cursor,
        acl_synced_at=row.acl_synced_at,
        unmapped_acl_count=row.unmapped_acl_count,
        acl_resync_required=bool(row.acl_resync_required),
    )


def to_document(row: models.Document) -> Document:
    """Map a ``documents`` row to the storage-faithful domain entity.

    Public because the permission chokepoint (``retrieval/queries``) issues the
    one permitted-document point read and must return the same domain type this
    repository does — a second mapper would be a second source of truth.
    """
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
        acl_enforced=row.acl_enforced,
        acl_principals=tuple(row.acl_principals) if row.acl_principals is not None else None,
        acl_synced_at=row.acl_synced_at,
        acl_scope_ids=tuple(row.acl_scope_ids) if row.acl_scope_ids is not None else None,
        external_id=row.external_id,
        kind=DocumentKind(row.kind),
        duration_ms=row.duration_ms,
        transcript_language=row.transcript_language,
        transcription_model=row.transcription_model,
    )


def _to_document_upload(row: models.DocumentUpload) -> DocumentUpload:
    return DocumentUpload(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        owner_id=row.owner_id,
        collection_id=row.collection_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        provider_upload_id=row.provider_upload_id,
        state=DocumentUploadState(row.state),
        part_size_bytes=row.part_size_bytes,
        part_count=row.part_count,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_modified_at=row.last_modified_at,
        error=row.error,
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
        time_start_ms=row.time_start_ms,
        time_end_ms=row.time_end_ms,
        transcript_segment_id=row.transcript_segment_id,
        speaker_id=row.speaker_id,
        speaker_name=row.speaker_name,
    )


def _to_transcript_speaker(row: models.TranscriptSpeaker) -> TranscriptSpeaker:
    return TranscriptSpeaker(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        speaker_id=row.speaker_id,
        display_name=row.display_name,
        name_status=row.name_status,
        name_confidence=row.name_confidence,
        name_method=row.name_method,
        evidence_segment_ids=tuple(UUID(value) for value in row.evidence_segment_ids),
    )


def _to_transcript_segment(row: models.TranscriptSegment) -> TranscriptSegment:
    return TranscriptSegment(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        ordinal=row.ordinal,
        speaker_id=row.speaker_id,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        char_start=row.char_start,
        char_end=row.char_end,
        text=row.text,
        confidence=row.confidence,
    )


def _to_transcription_checkpoint(
    row: models.TranscriptionCheckpoint,
) -> TranscriptionCheckpoint:
    return TranscriptionCheckpoint(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        model=row.model,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        language=row.language,
        words=tuple(dict(word) for word in row.words),
        created_at=row.created_at,
        updated_at=row.updated_at,
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
        custom_instructions=row.custom_instructions,
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
        # Lenient rehydration (spec 0006): a malformed stored payload yields
        # None and the message still renders as plain content — never a 500.
        question=AskUserQuestion.from_payload(row.question),
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
        time_start_ms=row.time_start_ms,
        time_end_ms=row.time_end_ms,
        transcript_segment_id=row.transcript_segment_id,
        speaker_id=row.speaker_id,
        speaker_name=row.speaker_name,
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


# The tenant's derived "All members" group (ADR-0022 §3). The name is a label
# for the admin UI — the group is identified by ``kind='system'``, never by name.
SYSTEM_GROUP_NAME = "All members"


def _to_group(row: models.Group) -> Group:
    return Group(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        kind=GroupKind(row.kind),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


def _to_mcp_server(row: models.McpServer) -> McpServer:
    return McpServer(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        name=row.name,
        transport=row.transport,
        endpoint_url=row.endpoint_url,
        auth_secret_ref=str(row.auth_secret_ref) if row.auth_secret_ref is not None else None,
        enabled=row.enabled,
        status=McpServerStatus(row.status),
        last_health_at=row.last_health_at,
        last_error=row.last_error,
        discovered_tools=list(row.discovered_tools or []),
        secret_hint=row.secret_hint,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_llm_provider(row: models.LlmProvider) -> LlmProvider:
    return LlmProvider(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        name=row.name,
        provider_type=row.provider_type,
        base_url=row.base_url,
        api_key_secret_ref=(
            str(row.api_key_secret_ref) if row.api_key_secret_ref is not None else None
        ),
        secret_hint=row.secret_hint,
        enabled=row.enabled,
        status=LlmProviderStatus(row.status),
        last_discovery_at=row.last_discovery_at,
        last_error=row.last_error,
        discovered_models=list(row.discovered_models or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_tenant_tool_policy(row: models.TenantToolPolicy) -> TenantToolPolicy:
    return TenantToolPolicy(
        id=row.id,
        tenant_id=row.tenant_id,
        tool_name=row.tool_name,
        enabled=row.enabled,
        requires_approval=row.requires_approval,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_tenant_sandbox_policy(row: models.TenantSandboxPolicy) -> TenantSandboxPolicy:
    return TenantSandboxPolicy(
        id=row.id,
        tenant_id=row.tenant_id,
        enabled=row.enabled,
        allowed_packages=tuple(row.allowed_packages or ()),
        denied_packages=tuple(row.denied_packages or ()),
        egress_allowed=row.egress_allowed,
        egress_allowlist=tuple(row.egress_allowlist or ()),
        max_runtime_s=row.max_runtime_s,
        max_memory_mb=row.max_memory_mb,
        daily_runtime_cap_s=row.daily_runtime_cap_s,
        max_concurrency=row.max_concurrency,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_tenant_autonomy_policy(row: models.TenantAutonomyPolicy) -> TenantAutonomyPolicy:
    return TenantAutonomyPolicy(
        id=row.id,
        tenant_id=row.tenant_id,
        max_autonomy=AutonomyLevel(row.max_autonomy),
        updated_by=row.updated_by,
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
        source_origin=row.source_origin,
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
        result_summary=row.result_summary,
        ordinal=row.ordinal,
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
        certification_state=CertificationState(row.certification_state),
        featured=bool(row.featured),
        category=row.category,
        disabled_at=row.disabled_at,
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


def _to_run_delivery(row: models.RunDelivery) -> RunDelivery:
    return RunDelivery(
        id=row.id,
        tenant_id=row.tenant_id,
        recipient_id=row.recipient_id,
        run_id=row.run_id,
        schedule_id=row.schedule_id,
        kind=RunDeliveryKind(row.kind),
        status=RunDeliveryStatus(row.status),
        summary=row.summary,
        created_at=row.created_at,
        read_at=row.read_at,
    )


def _to_code_run(row: models.CodeRun) -> CodeRun:
    return CodeRun(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        status=CodeRunStatus(row.status),
        code=row.code,
        stdout=row.stdout,
        stderr=row.stderr,
        artifact_ids=[UUID(x) for x in (row.artifact_ids or [])],
        requested_packages=tuple(row.requested_packages or []),
        resolved_packages=(
            tuple(row.resolved_packages) if row.resolved_packages is not None else None
        ),
        session_id=row.session_id,
        sandbox_session_id=row.sandbox_session_id,
        sandbox_generation=row.sandbox_generation,
        run_id=row.run_id,
        trace_id=row.trace_id,
        exit_code=row.exit_code,
        duration_ms=row.duration_ms,
        resource_usage=ResourceUsage.from_dict(row.resource_usage),
        image_digest=row.image_digest,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


def _to_sandbox_session(row: models.SandboxSession) -> SandboxSession:
    return SandboxSession(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        chat_session_id=row.chat_session_id,
        generation=row.generation,
        status=SandboxSessionStatus(row.status),
        image_digest=row.image_digest,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        closed_at=row.closed_at,
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

    async def set_fallback_models(
        self, tenant_id: UUID, *, fallback_models: list[str] | None
    ) -> Tenant | None:
        """Set (or clear) the tenant's turn-failover model list (ADR-0016 §4, #413).

        ``fallback_models`` is written as given: an ordered list of model ids the
        answer runtime fails over to when a model's retry budget is exhausted;
        ``None`` (or empty, normalised by the service to ``None``) clears it so
        answers fail exactly as before #413. Validation (each id allowed for this
        tenant, bounded length) is the ADMIN SERVICE's job — this is the write
        chokepoint only. Returns the updated entity, or ``None`` if no tenant
        with that id exists.
        """
        row = await self._session.get(models.Tenant, tenant_id)
        if row is None:
            return None
        row.fallback_models = fallback_models
        await self._session.flush()
        await self._session.refresh(row)
        return _to_tenant(row)

    async def set_logo_key(self, tenant_id: UUID, *, logo_key: str | None) -> Tenant | None:
        """Set (or clear) the tenant's per-tenant application-logo key (admin branding).

        ``logo_key`` is written as given: a non-null object-store key points the app
        shell at the tenant's uploaded logo, ``None`` clears it so the default brand
        mark applies again. Returns the updated entity, or ``None`` if no tenant with
        that id exists.
        """
        row = await self._session.get(models.Tenant, tenant_id)
        if row is None:
            return None
        row.logo_key = logo_key
        await self._session.flush()
        # Refresh so ``_to_tenant`` reads freshly-populated attributes rather than
        # triggering a lazy reload in a sync context (mirrors ``update``).
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

    async def get(self, user_id: UUID, *, refresh: bool = False) -> User | None:
        """Fetch a user (tenant-scoped). ``refresh=True`` bypasses the identity
        map (``populate_existing``) so the row reflects the DATABASE's current
        state — the OAuth callback's final role re-check uses it, where a
        session-cached row could mask a demotion committed elsewhere."""
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.id == user_id,
        )
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.email == email,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def attest_email(self, user_id: UUID, *, attested_by: UUID) -> User | None:
        """Record a tenant admin's identity attestation for this user (ADR-0019 §2).

        Idempotent from the caller's view: re-attesting refreshes
        ``email_attested_at``. Tenant-scoped (INV-1): a foreign-tenant user is
        invisible → ``None`` (the service maps that to 404). The audited event
        (``user.identity_attested``) is emitted by the calling service, not here.
        """
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.id == user_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.email_attested_at = datetime.now(UTC)
        row.email_attested_by = attested_by
        await self._session.flush()
        await self._session.refresh(row)
        return _to_user(row)

    async def attested_email_map(self) -> dict[str, UUID]:
        """Case-folded email → user id, **attested users only** (ADR-0019 §2/§4).

        The sync-time identity snapshot the framework freezes into the
        :class:`~app.connectors.base.AclMappingContext`: only users carrying an
        audited identity attestation (``email_attested_at`` set) participate in
        connector-ACL mapping — an unattested email maps to nothing (the
        deliberate under-share). Tenant-scoped (INV-1).
        """
        stmt = select(models.User.email, models.User.id).where(
            models.User.tenant_id == self._tenant_id,
            models.User.email_attested_at.is_not(None),
        )
        rows = (await self._session.execute(stmt)).all()
        return {str(row[0]).casefold(): row[1] for row in rows}

    async def set_avatar_key(self, user_id: UUID, *, avatar_key: str | None) -> User | None:
        """Set (or clear) the user's per-user profile-avatar key.

        ``avatar_key`` is written as given: a non-null object-store key points the
        shell at the user's uploaded avatar, ``None`` clears it so the initials
        fallback applies again. Tenant-scoped (INV-1): a user in another tenant is
        invisible here, so one user can never touch another's avatar. Returns the
        updated entity, or ``None`` if no such user exists in this tenant.
        """
        stmt = select(models.User).where(
            models.User.tenant_id == self._tenant_id,
            models.User.id == user_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.avatar_key = avatar_key
        await self._session.flush()
        await self._session.refresh(row)
        return _to_user(row)


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

        A thin wrapper over :meth:`update` that touches only ``default_model``,
        leaving ``custom_instructions`` unchanged (the ``_UNSET`` sentinel). Kept as
        a named method because ``PATCH /preferences`` and the chat session-create path
        both set the default model directly.
        """
        return await self.update(user_id, default_model=default_model)

    async def set_custom_instructions(
        self, user_id: UUID, custom_instructions: str | None
    ) -> UserPreferences:
        """Upsert the user's custom instructions (``None`` clears them).

        A thin wrapper over :meth:`update` that touches only ``custom_instructions``,
        leaving ``default_model`` unchanged.
        """
        return await self.update(user_id, custom_instructions=custom_instructions)

    async def update(
        self,
        user_id: UUID,
        *,
        default_model: str | None | _Unset = _UNSET,
        custom_instructions: str | None | _Unset = _UNSET,
    ) -> UserPreferences:
        """Lazy upsert of the user's preferences row (the ``(tenant_id, user_id)`` singleton).

        Tri-state per field: pass a value (incl. ``None`` to clear) to set it, or omit
        it (``_UNSET``) to leave it unchanged. Creates the row on first write, else
        updates the existing one, and returns the persisted state. Tenant-scoped
        (INV-1): a foreign-tenant/other-user row is invisible, so one user can never
        read or clobber another's preferences.
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
                default_model=None if isinstance(default_model, _Unset) else default_model,
                custom_instructions=(
                    None if isinstance(custom_instructions, _Unset) else custom_instructions
                ),
            )
            self._session.add(row)
        else:
            if not isinstance(default_model, _Unset):
                row.default_model = default_model
            if not isinstance(custom_instructions, _Unset):
                row.custom_instructions = custom_instructions
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

    async def get(self, collection_id: UUID, *, lock: bool = False) -> Collection | None:
        stmt = select(models.Collection).where(
            models.Collection.tenant_id == self._tenant_id,
            models.Collection.id == collection_id,
        )
        if lock:
            stmt = stmt.with_for_update()
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

    async def count_documents_for(self, collection_ids: Sequence[UUID]) -> dict[UUID, int]:
        """Document counts per collection, batched (one GROUP BY — no N+1, #526).

        The list projection needs a ``document_count`` for every row; resolved
        one id at a time that is a serial aggregate per collection in the page.
        Tenant-scoped (INV-1). An empty collection is simply **absent**, and the
        caller defaults to ``0`` — the same answer :meth:`count_documents` gives.
        The single-id form stays for the one-collection paths.
        """
        if not collection_ids:
            return {}
        stmt = (
            select(models.Document.collection_id, func.count())
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.collection_id.in_(list(collection_ids)),
            )
            .group_by(models.Document.collection_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

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

    async def begin_connect(self, source_id: UUID) -> Source | None:
        """Atomically advance the source's connect generation (ADR-0019 §1).

        Starting flow N+1 invalidates flow N: the incremented value is stamped
        into the new state record, and the callback finalize is a CAS on this
        exact value. The increment is a single ``UPDATE … SET
        connect_generation = connect_generation + 1 RETURNING`` — never a
        read-then-add, so two concurrent initiations can never mint the same
        generation. Returns the updated entity, or ``None`` when the source is
        not in this tenant.
        """
        stmt = (
            update(models.Source)
            .where(
                models.Source.tenant_id == self._tenant_id,
                models.Source.id == source_id,
            )
            .values(connect_generation=models.Source.connect_generation + 1)
            .returning(models.Source)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        await self._session.flush()
        return _to_source(row)

    # Statuses a callback may finalize from (ADR-0019 §1): awaiting consent, a
    # reauthorize after a failure, a reconnect of a connected-but-idle source.
    # A mid-``syncing`` source loses the CAS — the flow completes nothing.
    _CONNECTABLE_STATUSES = (
        SourceStatus.PENDING_AUTH.value,
        SourceStatus.PENDING.value,
        SourceStatus.READY.value,
        SourceStatus.ERROR.value,
    )

    async def complete_connect(
        self,
        source_id: UUID,
        *,
        expected_generation: int,
        auth_secret_ref: UUID,
        connected_account: dict[str, object],
    ) -> Source | None:
        """CAS-finalize a completed OAuth consent (ADR-0019 §1).

        One atomic ``UPDATE`` guarded on tenant, id, **the exact connect
        generation the flow was minted with**, and a connectable status: a flow
        superseded after the callback's earlier checks — or a source that
        started syncing / was deleted meanwhile — loses the CAS and this
        returns ``None`` (the caller then writes no credential binding and
        reports the flow denied). On success binds the vault reference +
        provider-account metadata (never token material), clears any prior
        failure, and moves the source to ``pending``.
        """
        stmt = (
            update(models.Source)
            .where(
                models.Source.tenant_id == self._tenant_id,
                models.Source.id == source_id,
                models.Source.connect_generation == expected_generation,
                models.Source.status.in_(self._CONNECTABLE_STATUSES),
            )
            .values(
                auth_secret_ref=auth_secret_ref,
                connected_account=connected_account,
                status=SourceStatus.PENDING.value,
                last_error=None,
            )
            .returning(models.Source)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        await self._session.flush()
        return _to_source(row)

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

    async def set_sync_cursor(self, source_id: UUID, cursor: str | None) -> None:
        """Persist the incremental-sync resume point (ADR-0019 §3).

        Called by the framework inside each page transaction (mutations + the
        advancing token commit atomically) and after a successful full sync
        (the pre-enumeration baseline). ``None`` clears the cursor — the next
        sync is a full resync (the HTTP-410 fallback). Tenant-scoped (INV-1);
        a missing row is a no-op (the sync task already owns the source).
        """
        stmt = (
            update(models.Source)
            .where(
                models.Source.tenant_id == self._tenant_id,
                models.Source.id == source_id,
            )
            .values(sync_cursor=cursor)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def record_acl_health(
        self,
        source_id: UUID,
        *,
        acl_synced_at: datetime | None,
        unmapped_acl_count: int | None,
    ) -> None:
        """Record the ACL-mirror health surface after a sync (ADR-0019 §2).

        ``acl_synced_at`` is the source-level "mirrored ACLs last refreshed"
        stamp; ``unmapped_acl_count`` is how many documents mapped to no Lumen
        principal (ingested but invisible — surfaced so an admin sees the
        silent-deny volume). Serialized onto the wire by the sources router.
        """
        stmt = (
            update(models.Source)
            .where(
                models.Source.tenant_id == self._tenant_id,
                models.Source.id == source_id,
            )
            .values(acl_synced_at=acl_synced_at, unmapped_acl_count=unmapped_acl_count)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def record_acl_resync_required(self, source_id: UUID, required: bool) -> None:
        """Persist the full-resync-required state (ADR-0019 §3).

        Set inside the **same transaction** as the source-wide stale stamp an
        ``integrity=incomplete`` page triggers, so the requirement survives a
        crash; cleared only by a run that has re-examined the whole corpus (a
        completed full sync). It deliberately outlives incremental retries: a
        page-level retry re-examines the documents that page reports, never the
        rows the source-wide stamp nulled, so it can never satisfy the
        requirement on its own.
        """
        stmt = (
            update(models.Source)
            .where(
                models.Source.tenant_id == self._tenant_id,
                models.Source.id == source_id,
            )
            .values(acl_resync_required=required)
        )
        await self._session.execute(stmt)
        await self._session.flush()

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
        document_id: UUID | None = None,
        owner_id: UUID,
        collection_id: UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        acl_enforced: bool,
        status: DocumentStatus = DocumentStatus.PENDING,
        source_id: UUID | None = None,
        external_id: str | None = None,
        acl_principals: Sequence[str] | None = None,
        acl_synced_at: datetime | None = None,
        acl_scope_ids: Sequence[str] | None = None,
        kind: DocumentKind = DocumentKind.DOCUMENT,
    ) -> Document:
        """Create a document row — the ACL-mode write seam (ADR-0019 §2).

        ``acl_enforced`` is deliberately **mandatory with no default**: the
        enforcement mode is never defaulted at write time. Callers derive it
        structurally — ``False`` for uploads/``web`` (owner-or-grant), ``True``
        for any document originating from a ``map_acl``-declaring connector
        (mirror-only; ownership/grants do not apply). A managed-source document
        persisted ``acl_enforced=False`` is a defect the write-mode tests pin.
        """
        row = models.Document(
            id=document_id or uuid4(),
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            collection_id=collection_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status=status.value,
            source_id=source_id,
            acl_enforced=acl_enforced,
            acl_principals=list(acl_principals) if acl_principals is not None else None,
            acl_synced_at=acl_synced_at,
            acl_scope_ids=list(acl_scope_ids) if acl_scope_ids is not None else None,
            external_id=external_id,
            kind=kind.value,
        )
        self._session.add(row)
        await self._session.flush()
        return to_document(row)

    async def update_media_metadata(
        self,
        document_id: UUID,
        *,
        kind: DocumentKind,
        duration_ms: int,
        transcript_language: str | None,
        transcription_model: str,
        ingestion_run_id: UUID | None = None,
    ) -> Document | None:
        """Persist validated media/transcription provenance, tenant-scoped.

        Ingestion passes its durable run token. The optional form remains for
        administrative/test metadata setup, while the worker path is fenced on
        both ``processing`` and the exact claimant token.
        """
        predicates = [
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        ]
        if ingestion_run_id is not None:
            predicates.extend(
                [
                    models.Document.status == DocumentStatus.PROCESSING.value,
                    models.Document.ingestion_run_id == ingestion_run_id,
                ]
            )
        stmt = select(models.Document).where(*predicates)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.kind = kind.value
        row.duration_ms = duration_ms
        row.transcript_language = transcript_language
        row.transcription_model = transcription_model
        await self._session.flush()
        await self._session.refresh(row)
        return to_document(row)

    async def get_by_external_id(self, source_id: UUID, external_id: str) -> Document | None:
        """The source's document for a provider id, or ``None`` (ADR-0019 §3).

        The identity-reconcile lookup: incremental upserts key on
        ``(source_id, external_id)`` (unique when set) instead of wholesale
        delete-and-recreate. Tenant-scoped (INV-1).
        """
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.source_id == source_id,
            models.Document.external_id == external_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_document(row) if row is not None else None

    async def update_from_sync(
        self,
        document_id: UUID,
        *,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        status: DocumentStatus,
        acl_principals: Sequence[str] | None,
        acl_synced_at: datetime | None,
        acl_scope_ids: Sequence[str] | None,
    ) -> Document | None:
        """Refresh an existing connector document in place (incremental upsert).

        Keeps the row id stable (the index replaces chunk docs by document id,
        so no orphans) while replacing content metadata + the mirrored ACL.
        ``acl_enforced`` is intentionally NOT a parameter: the mode is set once
        at create from the connector's declared capability and never flips.
        Tenant-scoped; returns ``None`` when no row matches (INV-1).
        """
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.filename = filename
        row.mime_type = mime_type
        row.size_bytes = size_bytes
        row.storage_key = storage_key
        row.status = status.value
        row.error = None
        row.acl_principals = list(acl_principals) if acl_principals is not None else None
        row.acl_synced_at = acl_synced_at
        row.acl_scope_ids = list(acl_scope_ids) if acl_scope_ids is not None else None
        await self._session.flush()
        await self._session.refresh(row)
        return to_document(row)

    async def stamp_acl_stale_by_scope(
        self, source_id: UUID, scope_ids: Iterable[str]
    ) -> list[UUID]:
        """Stale-stamp every known descendant of the given containers (ADR-0019 §3).

        Sets ``acl_synced_at → NULL`` on this source's ``acl_enforced``
        documents whose persisted scope chain intersects ``scope_ids`` — the
        freshness predicate then denies them **immediately**, not at window
        expiry. Runs inside the caller's page transaction. Returns the affected
        document ids so the caller can propagate the stamp to the search index
        (update-by-query). Portable across Postgres and the offline SQLite: the
        scope-chain intersection is evaluated in Python over the source's rows
        (a bounded set — one source's corpus), not via a dialect-specific JSON
        operator.
        """
        wanted = set(scope_ids)
        if not wanted:
            return []
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.source_id == source_id,
            models.Document.acl_enforced.is_(True),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        stamped: list[UUID] = []
        for row in rows:
            chain = set(row.acl_scope_ids or [])
            if chain & wanted:
                row.acl_synced_at = None
                stamped.append(row.id)
        if stamped:
            await self._session.flush()
        return stamped

    async def stamp_acl_stale_for_source(self, source_id: UUID) -> list[UUID]:
        """Stale-stamp EVERY ``acl_enforced`` document of a source (fail closed).

        The ADR-0019 §3 source-wide stamp for a page whose cascade effects are
        unprovable (``integrity=incomplete``): every mirrored document is denied
        immediately until a later sync re-examines it. Returns the affected ids
        for the index propagation.
        """
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.source_id == source_id,
            models.Document.acl_enforced.is_(True),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        stamped: list[UUID] = []
        for row in rows:
            row.acl_synced_at = None
            stamped.append(row.id)
        if stamped:
            await self._session.flush()
        return stamped

    async def attest_acl_unchanged(
        self, source_id: UUID, *, attested_at: datetime, exclude_ids: Iterable[UUID] = ()
    ) -> list[UUID]:
        """Attest untouched mirrors after a gap-free replay (ADR-0019 §2/§3).

        A complete, gap-free change-log replay **proves** the documents it did
        not report are unchanged, so their mirrors are re-attested — otherwise
        an hourly successful replay would let every untouched document age past
        ``CONNECTOR_ACL_MAX_AGE_HOURS`` and silently vanish from retrieval.

        The one row a replay may **never** revive is one an *incomplete* run
        stamped stale: that stamp is recorded as ``acl_synced_at IS NULL``, and
        this update only ever advances a **non-NULL** timestamp. The
        distinction is therefore structural, not bookkeeping — a stale-stamped
        document stays denied until a sync re-examines it for real.
        ``exclude_ids`` skips the documents this run already re-examined (they
        carry their own fresh stamp). Returns the ids actually advanced so the
        caller can propagate the freshness to the search index.
        """
        skip = set(exclude_ids)
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.source_id == source_id,
            models.Document.acl_enforced.is_(True),
            models.Document.acl_synced_at.is_not(None),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        attested: list[UUID] = []
        for row in rows:
            if row.id in skip:
                continue
            row.acl_synced_at = attested_at
            attested.append(row.id)
        if attested:
            await self._session.flush()
        return attested

    async def count_stale_acl(self, source_id: UUID) -> int:
        """Mirrored documents of ``source_id`` still stamped stale (ADR-0019 §3).

        ``acl_synced_at IS NULL`` on an ``acl_enforced`` row means "denied until
        a sync re-examines this for real" — the state a cascade or an incomplete
        run leaves behind. The sync terminal uses this as its **proof
        obligation**: a run may only publish a ready/fresh source when this is
        zero, because source-level freshness over a NULL row would advertise
        health for content the permission predicate is (correctly) denying.
        Tenant-scoped (INV-1).
        """
        stmt = (
            select(func.count())
            .select_from(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.source_id == source_id,
                models.Document.acl_enforced.is_(True),
                models.Document.acl_synced_at.is_(None),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

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
        return [to_document(r) for r in rows]

    async def get(self, document_id: UUID) -> Document | None:
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_document(row) if row is not None else None

    async def get_for_update(self, document_id: UUID) -> Document | None:
        """Tenant-scoped row lock for exactly-once worker transitions."""
        stmt = (
            select(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.id == document_id,
            )
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_document(row) if row is not None else None

    async def get_claimed_for_update(
        self, document_id: UUID, ingestion_run_id: UUID
    ) -> Document | None:
        """Lock and return only the PROCESSING row owned by this ingestion run."""
        stmt = (
            select(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.id == document_id,
                models.Document.status == DocumentStatus.PROCESSING.value,
                models.Document.ingestion_run_id == ingestion_run_id,
            )
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_document(row) if row is not None else None

    async def get_many(self, document_ids: Iterable[UUID]) -> dict[UUID, Document]:
        """Fetch many documents by id in **one** query — the batch form of :meth:`get`.

        For callers holding a page of rows that reference documents (search
        result enrichment, #514): a lookup per id puts a serialized round-trip on
        the critical path for every distinct document.

        Tenant-scoped exactly like :meth:`get` (INV-1). An id belonging to
        another tenant, or one that no longer exists, is simply **absent** from
        the mapping — so a caller keying off the result drops it the same way a
        per-id ``None`` did, with no way to mistake a miss for a hit. Returned as
        a mapping rather than a list so no caller can depend on row order.
        """
        # Duplicates collapse (a page routinely holds many chunks of one
        # document) and an empty page skips the query entirely.
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return {}
        stmt = select(models.Document).where(
            models.Document.tenant_id == self._tenant_id,
            models.Document.id.in_(ids),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.id: to_document(row) for row in rows}

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
        return [to_document(r) for r in rows]

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
        return [to_document(r) for r in rows]

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

    async def count_chunks_for(self, document_ids: Sequence[UUID]) -> dict[UUID, int]:
        """Chunk counts per document, batched (one GROUP BY — no N+1, #526).

        The list projection needs a ``chunk_count`` for every row; resolved one
        id at a time that is a serial aggregate over ``chunks`` — the largest
        table — per document in the page. Tenant-scoped (INV-1). A document with
        no chunks is simply **absent**, and the caller defaults to ``0`` — the
        same answer :meth:`count_chunks` gives, since a document only gains rows
        once ingestion (#21) runs. The single-id form stays for the one-document
        paths.
        """
        if not document_ids:
            return {}
        stmt = (
            select(models.Chunk.document_id, func.count())
            .where(
                models.Chunk.tenant_id == self._tenant_id,
                models.Chunk.document_id.in_(list(document_ids)),
            )
            .group_by(models.Chunk.document_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def count_by_storage_key(self, storage_key: str) -> int:
        """Count this tenant's documents backed by ``storage_key``.

        Some connector/legacy objects are content-addressed and may be shared;
        direct multipart objects have unique quarantine keys. Before deleting a
        stored object for any removed document, callers check this is ``0`` so
        they never delete bytes another live document still references (INV-1:
        the query remains tenant-scoped).
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
        return to_document(row)

    async def claim_ingestion(
        self,
        document_id: UUID,
        *,
        ingestion_run_id: UUID,
        stale_before: datetime,
    ) -> Document | None:
        """Atomically claim pending/ready work or take over a stale PROCESSING lease.

        A single conditional ``UPDATE … RETURNING`` is the admission gate. Fresh
        concurrent/redelivered deliveries lose without doing provider work;
        recovery may replace only a lease whose heartbeat is older than the
        caller-supplied settings-derived threshold. READY remains claimable for
        explicit/idempotent re-ingestion; media reuses its paid checkpoints.
        """
        stmt = (
            update(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.id == document_id,
                or_(
                    models.Document.status.in_(
                        (DocumentStatus.PENDING.value, DocumentStatus.READY.value)
                    ),
                    and_(
                        models.Document.status == DocumentStatus.PROCESSING.value,
                        models.Document.updated_at < stale_before,
                    ),
                ),
            )
            .values(
                status=DocumentStatus.PROCESSING.value,
                error=None,
                ingestion_run_id=ingestion_run_id,
                updated_at=func.now(),
            )
            .returning(models.Document)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return to_document(row) if row is not None else None

    async def touch_processing(self, document_id: UUID, ingestion_run_id: UUID) -> bool:
        """Refresh this claimant's live lease; return false after takeover/terminalization."""
        row_id = (
            await self._session.execute(
                update(models.Document)
                .where(
                    models.Document.tenant_id == self._tenant_id,
                    models.Document.id == document_id,
                    models.Document.status == DocumentStatus.PROCESSING.value,
                    models.Document.ingestion_run_id == ingestion_run_id,
                )
                .values(updated_at=func.now())
                .returning(models.Document.id)
            )
        ).scalar_one_or_none()
        await self._session.flush()
        return row_id is not None

    async def finish_ingestion(
        self,
        document_id: UUID,
        *,
        ingestion_run_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
    ) -> Document | None:
        """CAS one claimant to READY/FAILED and clear its durable lease token."""
        if status not in (DocumentStatus.READY, DocumentStatus.FAILED):
            raise ValueError("finish_ingestion requires a terminal document status")
        stmt = (
            update(models.Document)
            .where(
                models.Document.tenant_id == self._tenant_id,
                models.Document.id == document_id,
                models.Document.status == DocumentStatus.PROCESSING.value,
                models.Document.ingestion_run_id == ingestion_run_id,
            )
            .values(
                status=status.value,
                error=error,
                ingestion_run_id=None,
                updated_at=func.now(),
            )
            .returning(models.Document)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        return to_document(row) if row is not None else None

    async def release_ingestion(self, document_id: UUID, ingestion_run_id: UUID) -> bool:
        """Conditionally release this claimant to PENDING before Celery retry."""
        row_id = (
            await self._session.execute(
                update(models.Document)
                .where(
                    models.Document.tenant_id == self._tenant_id,
                    models.Document.id == document_id,
                    models.Document.status == DocumentStatus.PROCESSING.value,
                    models.Document.ingestion_run_id == ingestion_run_id,
                )
                .values(
                    status=DocumentStatus.PENDING.value,
                    error=None,
                    ingestion_run_id=None,
                    updated_at=func.now(),
                )
                .returning(models.Document.id)
            )
        ).scalar_one_or_none()
        await self._session.flush()
        return row_id is not None


class DocumentUploadRepository(_TenantScopedRepository):
    """Owner/tenant-scoped multipart sessions; provider ids never escape services."""

    async def create(
        self,
        *,
        upload_id: UUID,
        document_id: UUID,
        owner_id: UUID,
        collection_id: UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        provider_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        expires_at: datetime,
        last_modified_at: datetime | None = None,
    ) -> DocumentUpload:
        row = models.DocumentUpload(
            id=upload_id,
            tenant_id=self._tenant_id,
            document_id=document_id,
            owner_id=owner_id,
            collection_id=collection_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            provider_upload_id=provider_upload_id,
            state=DocumentUploadState.INITIATED.value,
            part_size_bytes=part_size_bytes,
            part_count=part_count,
            expires_at=expires_at,
            last_modified_at=last_modified_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_document_upload(row)

    async def get_for_owner(
        self, upload_id: UUID, owner_id: UUID, *, lock: bool = False
    ) -> DocumentUpload | None:
        stmt = select(models.DocumentUpload).where(
            models.DocumentUpload.tenant_id == self._tenant_id,
            models.DocumentUpload.id == upload_id,
            models.DocumentUpload.owner_id == owner_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_document_upload(row) if row is not None else None

    async def list_active_for_collection(
        self, collection_id: UUID, owner_id: UUID, *, lock: bool = False
    ) -> list[DocumentUpload]:
        """Active provider sessions that must be aborted before collection delete."""
        stmt = (
            select(models.DocumentUpload)
            .where(
                models.DocumentUpload.tenant_id == self._tenant_id,
                models.DocumentUpload.collection_id == collection_id,
                models.DocumentUpload.owner_id == owner_id,
                models.DocumentUpload.state.in_(
                    [
                        DocumentUploadState.INITIATED.value,
                        DocumentUploadState.COMPLETING.value,
                    ]
                ),
            )
            .order_by(models.DocumentUpload.created_at.asc())
        )
        if lock:
            stmt = stmt.with_for_update()
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document_upload(row) for row in rows]

    async def set_state(
        self,
        upload_id: UUID,
        owner_id: UUID,
        state: DocumentUploadState,
        *,
        error: str | None = None,
    ) -> DocumentUpload | None:
        stmt = select(models.DocumentUpload).where(
            models.DocumentUpload.tenant_id == self._tenant_id,
            models.DocumentUpload.id == upload_id,
            models.DocumentUpload.owner_id == owner_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.state = state.value
        row.error = error
        await self._session.flush()
        await self._session.refresh(row)
        return _to_document_upload(row)

    async def delete(self, upload_id: UUID, owner_id: UUID) -> bool:
        """Remove a terminal/control-plane row after provider cleanup."""
        row = (
            await self._session.execute(
                select(models.DocumentUpload).where(
                    models.DocumentUpload.tenant_id == self._tenant_id,
                    models.DocumentUpload.id == upload_id,
                    models.DocumentUpload.owner_id == owner_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def delete_for_document(self, document_id: UUID) -> bool:
        """Remove the private upload-control record once its document is deleted."""
        row = (
            await self._session.execute(
                select(models.DocumentUpload).where(
                    models.DocumentUpload.tenant_id == self._tenant_id,
                    models.DocumentUpload.document_id == document_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def list_expired(self, *, now: datetime, limit: int) -> list[DocumentUpload]:
        stmt = (
            select(models.DocumentUpload)
            .where(
                models.DocumentUpload.tenant_id == self._tenant_id,
                models.DocumentUpload.expires_at <= now,
                models.DocumentUpload.state.in_(
                    [DocumentUploadState.INITIATED.value, DocumentUploadState.COMPLETING.value]
                ),
            )
            .order_by(models.DocumentUpload.expires_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document_upload(row) for row in rows]


class DocumentUploadReconcileRepository:
    """Cross-tenant expired-session discovery for the system janitor only.

    The caller binds the RLS bypass sentinel, then processes every returned row
    through a tenant-bound transaction. Request paths never construct this type.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_expired(self, *, now: datetime, limit: int) -> list[DocumentUpload]:
        stmt = (
            select(models.DocumentUpload)
            .where(
                models.DocumentUpload.expires_at <= now,
                models.DocumentUpload.state.in_(
                    [DocumentUploadState.INITIATED.value, DocumentUploadState.COMPLETING.value]
                ),
            )
            .order_by(models.DocumentUpload.expires_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_document_upload(row) for row in rows]


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
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None


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
        time_start_ms: int | None = None,
        time_end_ms: int | None = None,
        transcript_segment_id: UUID | None = None,
        speaker_id: str | None = None,
        speaker_name: str | None = None,
    ) -> Chunk:
        row = models.Chunk(
            tenant_id=self._tenant_id,
            document_id=document_id,
            ord=ord,
            text=text,
            char_start=char_start,
            char_end=char_end,
            embedding=list(embedding) if embedding is not None else None,
            time_start_ms=time_start_ms,
            time_end_ms=time_end_ms,
            transcript_segment_id=transcript_segment_id,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
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
                time_start_ms=chunk.time_start_ms,
                time_end_ms=chunk.time_end_ms,
                transcript_segment_id=chunk.transcript_segment_id,
                speaker_id=chunk.speaker_id,
                speaker_name=chunk.speaker_name,
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


@dataclass(frozen=True, slots=True)
class TranscriptSpeakerInput:
    speaker_id: str
    display_name: str | None = None
    name_status: str = "unknown"
    name_confidence: float | None = None
    name_method: str | None = None
    evidence_segment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptSegmentInput:
    id: UUID
    ordinal: int
    speaker_id: str
    start_ms: int
    end_ms: int
    char_start: int
    char_end: int
    text: str
    confidence: float | None = None


class TranscriptRepository(_TenantScopedRepository):
    """Normalized diarized speakers/segments for one tenant (spec 0008 §4)."""

    async def replace_for_document(
        self,
        document_id: UUID,
        *,
        speakers: Sequence[TranscriptSpeakerInput],
        segments: Sequence[TranscriptSegmentInput],
    ) -> tuple[list[TranscriptSpeaker], list[TranscriptSegment]]:
        """Atomically replace a transcript; a foreign document writes nothing."""
        document = (
            await self._session.execute(
                select(models.Document).where(
                    models.Document.tenant_id == self._tenant_id,
                    models.Document.id == document_id,
                )
            )
        ).scalar_one_or_none()
        if document is None:
            return [], []
        if document.kind not in {DocumentKind.AUDIO.value, DocumentKind.VIDEO.value}:
            raise ValueError("transcripts can only be stored for media documents")
        if document.duration_ms is None or document.duration_ms <= 0:
            raise ValueError("media duration must be stored before its transcript")
        if not segments:
            raise ValueError("a media transcript must contain at least one segment")

        speaker_ids = {speaker.speaker_id for speaker in speakers}
        segment_ids = {segment.id for segment in segments}
        if len(speaker_ids) != len(speakers):
            raise ValueError("transcript speaker ids must be unique")
        if len(segment_ids) != len(segments):
            raise ValueError("transcript segment ids must be unique")
        if [segment.ordinal for segment in segments] != list(range(len(segments))):
            raise ValueError("transcript ordinals must be contiguous from zero")
        if speaker_ids != {segment.speaker_id for segment in segments}:
            raise ValueError("transcript speakers must exactly match segment speakers")
        prior_start = -1
        prior_end = -1
        expected_char_start = 0
        for segment in segments:
            if segment.speaker_id not in speaker_ids:
                raise ValueError("transcript segment references an unknown speaker")
            if not (0 <= segment.start_ms < segment.end_ms <= document.duration_ms):
                raise ValueError("transcript segment has an invalid time span")
            if segment.start_ms < prior_start or segment.end_ms < prior_end:
                raise ValueError("transcript segment timing must be ordered")
            if (
                segment.char_start != expected_char_start
                or segment.char_end != segment.char_start + len(segment.text)
                or not segment.text
            ):
                raise ValueError("transcript character spans must match canonical text")
            if segment.confidence is not None and not 0 <= segment.confidence <= 1:
                raise ValueError("transcript segment confidence must be between zero and one")
            prior_start = segment.start_ms
            prior_end = segment.end_ms
            expected_char_start = segment.char_end + 1
        for speaker in speakers:
            evidence_ids = set(speaker.evidence_segment_ids)
            if len(evidence_ids) != len(speaker.evidence_segment_ids):
                raise ValueError("speaker-name evidence ids must be unique")
            if not evidence_ids <= segment_ids:
                raise ValueError("speaker-name evidence references an unknown segment")
            if speaker.name_status == "unknown":
                if (
                    any(
                        value is not None
                        for value in (
                            speaker.display_name,
                            speaker.name_confidence,
                            speaker.name_method,
                        )
                    )
                    or speaker.evidence_segment_ids
                ):
                    raise ValueError("unknown speakers cannot carry inferred-name evidence")
            elif speaker.name_status == "inferred":
                if (
                    speaker.display_name is None
                    or not speaker.display_name.strip()
                    or speaker.name_confidence is None
                    or not 0 <= speaker.name_confidence <= 1
                    or speaker.name_method not in {"self_introduction", "contextual_dialogue"}
                    or not speaker.evidence_segment_ids
                ):
                    raise ValueError("inferred speaker names require coherent evidence")
            else:
                raise ValueError("speaker name status is invalid")

        old_speakers = (
            (
                await self._session.execute(
                    select(models.TranscriptSpeaker).where(
                        models.TranscriptSpeaker.tenant_id == self._tenant_id,
                        models.TranscriptSpeaker.document_id == document_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        old_segments = (
            (
                await self._session.execute(
                    select(models.TranscriptSegment).where(
                        models.TranscriptSegment.tenant_id == self._tenant_id,
                        models.TranscriptSegment.document_id == document_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for old_speaker in old_speakers:
            await self._session.delete(old_speaker)
        for old_segment in old_segments:
            await self._session.delete(old_segment)
        await self._session.flush()

        segment_rows = [
            models.TranscriptSegment(
                id=segment.id,
                tenant_id=self._tenant_id,
                document_id=document_id,
                ordinal=segment.ordinal,
                speaker_id=segment.speaker_id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                char_start=segment.char_start,
                char_end=segment.char_end,
                text=segment.text,
                confidence=segment.confidence,
            )
            for segment in segments
        ]
        speaker_rows = [
            models.TranscriptSpeaker(
                tenant_id=self._tenant_id,
                document_id=document_id,
                speaker_id=speaker.speaker_id,
                display_name=speaker.display_name,
                name_status=speaker.name_status,
                name_confidence=speaker.name_confidence,
                name_method=speaker.name_method,
                evidence_segment_ids=[str(value) for value in speaker.evidence_segment_ids],
            )
            for speaker in speakers
        ]
        self._session.add_all([*segment_rows, *speaker_rows])
        await self._session.flush()
        return (
            [_to_transcript_speaker(row) for row in speaker_rows],
            [_to_transcript_segment(row) for row in segment_rows],
        )

    async def list_speakers(self, document_id: UUID) -> list[TranscriptSpeaker]:
        stmt = (
            select(models.TranscriptSpeaker)
            .where(
                models.TranscriptSpeaker.tenant_id == self._tenant_id,
                models.TranscriptSpeaker.document_id == document_id,
            )
            .order_by(models.TranscriptSpeaker.speaker_id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_transcript_speaker(row) for row in rows]

    async def list_segments(
        self,
        document_id: UUID,
        *,
        after_ordinal: int | None = None,
        around_ms: int | None = None,
        limit: int | None = None,
    ) -> list[TranscriptSegment]:
        start_ordinal = after_ordinal + 1 if after_ordinal is not None else 0
        if around_ms is not None:
            containing = (
                await self._session.execute(
                    select(models.TranscriptSegment.ordinal)
                    .where(
                        models.TranscriptSegment.tenant_id == self._tenant_id,
                        models.TranscriptSegment.document_id == document_id,
                        models.TranscriptSegment.end_ms > around_ms,
                    )
                    .order_by(models.TranscriptSegment.ordinal.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if containing is not None:
                start_ordinal = max(start_ordinal, containing)
        stmt = (
            select(models.TranscriptSegment)
            .where(
                models.TranscriptSegment.tenant_id == self._tenant_id,
                models.TranscriptSegment.document_id == document_id,
                models.TranscriptSegment.ordinal >= start_ordinal,
            )
            .order_by(models.TranscriptSegment.ordinal.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_transcript_segment(row) for row in rows]


class TranscriptionCheckpointRepository(_TenantScopedRepository):
    """Idempotent paid STT chunk checkpoints, persisted before embedding."""

    async def get(
        self, document_id: UUID, *, chunk_index: int, model: str
    ) -> TranscriptionCheckpoint | None:
        stmt = select(models.TranscriptionCheckpoint).where(
            models.TranscriptionCheckpoint.tenant_id == self._tenant_id,
            models.TranscriptionCheckpoint.document_id == document_id,
            models.TranscriptionCheckpoint.chunk_index == chunk_index,
            models.TranscriptionCheckpoint.model == model,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_transcription_checkpoint(row) if row is not None else None

    async def upsert(
        self,
        document_id: UUID,
        *,
        ingestion_run_id: UUID | None = None,
        chunk_index: int,
        model: str,
        start_ms: int,
        end_ms: int,
        language: str | None,
        words: Sequence[dict[str, object]],
    ) -> TranscriptionCheckpoint | None:
        document_predicates = [
            models.Document.tenant_id == self._tenant_id,
            models.Document.id == document_id,
        ]
        if ingestion_run_id is not None:
            document_predicates.extend(
                [
                    models.Document.status == DocumentStatus.PROCESSING.value,
                    models.Document.ingestion_run_id == ingestion_run_id,
                ]
            )
        document_exists = (
            await self._session.execute(
                select(models.Document.id).where(*document_predicates).with_for_update()
            )
        ).scalar_one_or_none()
        if document_exists is None:
            return None
        stmt = select(models.TranscriptionCheckpoint).where(
            models.TranscriptionCheckpoint.tenant_id == self._tenant_id,
            models.TranscriptionCheckpoint.document_id == document_id,
            models.TranscriptionCheckpoint.chunk_index == chunk_index,
            models.TranscriptionCheckpoint.model == model,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = models.TranscriptionCheckpoint(
                tenant_id=self._tenant_id,
                document_id=document_id,
                chunk_index=chunk_index,
                model=model,
                start_ms=start_ms,
                end_ms=end_ms,
                language=language,
                words=[dict(word) for word in words],
            )
            self._session.add(row)
        else:
            row.start_ms = start_ms
            row.end_ms = end_ms
            row.language = language
            row.words = [dict(word) for word in words]
        await self._session.flush()
        await self._session.refresh(row)
        return _to_transcription_checkpoint(row)

    async def list_for_document(
        self, document_id: UUID, *, model: str
    ) -> list[TranscriptionCheckpoint]:
        stmt = (
            select(models.TranscriptionCheckpoint)
            .where(
                models.TranscriptionCheckpoint.tenant_id == self._tenant_id,
                models.TranscriptionCheckpoint.document_id == document_id,
                models.TranscriptionCheckpoint.model == model,
            )
            .order_by(models.TranscriptionCheckpoint.chunk_index.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_transcription_checkpoint(row) for row in rows]

    async def delete_other_models(
        self,
        document_id: UUID,
        *,
        keep_model: str,
        ingestion_run_id: UUID | None = None,
    ) -> int:
        if ingestion_run_id is not None:
            claimed = (
                await self._session.execute(
                    select(models.Document.id)
                    .where(
                        models.Document.tenant_id == self._tenant_id,
                        models.Document.id == document_id,
                        models.Document.status == DocumentStatus.PROCESSING.value,
                        models.Document.ingestion_run_id == ingestion_run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if claimed is None:
                return 0
        rows = (
            (
                await self._session.execute(
                    select(models.TranscriptionCheckpoint).where(
                        models.TranscriptionCheckpoint.tenant_id == self._tenant_id,
                        models.TranscriptionCheckpoint.document_id == document_id,
                        models.TranscriptionCheckpoint.model != keep_model,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)


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

    async def count_for_sessions(self, session_ids: Sequence[UUID]) -> dict[UUID, int]:
        """Message counts per session, batched (one GROUP BY — no N+1, #396).

        Tenant-scoped (INV-1). Sessions with no messages are simply absent —
        the caller defaults to ``0``. The single-id :meth:`count_messages`
        stays for the one-session paths.
        """
        if not session_ids:
            return {}
        stmt = (
            select(models.Message.session_id, func.count())
            .where(
                models.Message.tenant_id == self._tenant_id,
                models.Message.session_id.in_(list(session_ids)),
            )
            .group_by(models.Message.session_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

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

        One statement (#492 AC-2): a single ORM-enabled ``UPDATE ... WHERE
        tenant_id AND id`` — the tenant predicate rides the write itself (so a
        foreign/missing id matches zero rows and closes the SELECT→UPDATE TOCTOU
        window), replacing the prior read-modify-flush that redundantly reloaded a
        row the send path already holds. ``synchronize_session="evaluate"`` patches
        any already-loaded instance in the identity map so it is not left stale
        under ``expire_on_commit=False``. The timestamp stays a Python
        ``datetime.now(UTC)`` (NOT ``func.now()``, which is Postgres'
        ``transaction_timestamp()`` — the transaction's start, not the current
        instant), so the stamp is byte-identical to the prior behaviour.
        """
        stmt = (
            update(models.ChatSession)
            .where(
                models.ChatSession.tenant_id == self._tenant_id,
                models.ChatSession.id == session_id,
            )
            .values(updated_at=datetime.now(UTC))
            .execution_options(synchronize_session="evaluate")
        )
        await self._session.execute(stmt)

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
        question: AskUserQuestion | None = None,
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
            # The clarifying question this turn ended with, if any (spec 0006):
            # stored as the REST payload verbatim (AskUserQuestion.to_payload).
            question=question.to_payload() if question is not None else None,
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

    async def list_for_session_after(
        self,
        session_id: UUID,
        *,
        after_created_at: datetime,
        after_message_id: UUID,
        limit: int | None = None,
    ) -> list[Message]:
        """Messages STRICTLY NEWER than the coverage cursor, oldest first (#416).

        The cursor is the ``(created_at, id)`` total order (#446 finding 5) —
        newer means a later timestamp, or the same timestamp with a different,
        later-inserted id EXCLUDED conservatively: on a timestamp tie only the
        boundary row itself is excluded (id inequality cannot order same-second
        peers), so a tied peer is RESENT verbatim rather than silently dropped
        — duplication-safe, loss-unsafe never. Valid after the boundary row is
        pruned (the comparison never needs the row).
        """
        # The tie branch uses a ±1s TOLERANCE window (SQLite's server-default
        # timestamps are second-resolution strings while bound params carry
        # microseconds — an equality can never match) AND the ``(created_at,
        # id)`` TOTAL ORDER the summary CAS uses (#446 round-3 liveness): the
        # tie admits only LARGER ids, so batch selection and CAS acceptance
        # advance in the SAME order — a 100-same-second backlog covers in
        # id-ordered passes instead of starving. Same-second peers with a
        # SMALLER id than the boundary were covered by an earlier id-ordered
        # pass, so their exclusion is exact, not lossy. ``limit`` bounds the
        # fetch (the task's batch path must not materialize a whole backlog).
        window_start = after_created_at - timedelta(seconds=1)
        stmt = (
            select(models.Message)
            .where(
                models.Message.tenant_id == self._tenant_id,
                models.Message.session_id == session_id,
                or_(
                    models.Message.created_at > after_created_at,
                    and_(
                        models.Message.created_at > window_start,
                        models.Message.id > after_message_id,
                    ),
                ),
            )
            # (created_at, id) — the SAME total order the CAS compares, so a
            # batch's last element is always the maximum the CAS will accept
            # (#446 round-3 liveness; #439 tracks a truly chronological key).
            .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_message(r) for r in rows]

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
    without a per-row N+1.

    A citation was only ever WRITTEN for a permitted passage (INV-3), but that is a
    fact about write time, not read time: a grant revoked afterwards leaves the row
    in place. This join is tenant-scoped only, so callers on a read path must
    re-check permission and :meth:`redacted` the ones that no longer pass (#536).
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
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    #: True when the reader may no longer retrieve the cited document, in which
    #: case ``snippet`` and ``document_name`` have been emptied. The row itself is
    #: kept so a claim's provenance stays visible rather than silently vanishing.
    redacted: bool = False

    def redact(self) -> CitationView:
        """This citation with everything disclosing removed, shell intact."""
        return replace(
            self,
            snippet="",
            document_name="",
            time_start_ms=None,
            time_end_ms=None,
            transcript_segment_id=None,
            speaker_id=None,
            speaker_name=None,
            redacted=True,
        )


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
        time_start_ms: int | None = None,
        time_end_ms: int | None = None,
        transcript_segment_id: UUID | None = None,
        speaker_id: str | None = None,
        speaker_name: str | None = None,
    ) -> Citation:
        if (time_start_ms is None) != (time_end_ms is None):
            raise ValueError("citation timestamp fields must be supplied as a pair")
        source = (
            await self._session.execute(
                select(models.Chunk, models.Document)
                .join(models.Document, models.Document.id == models.Chunk.document_id)
                .where(
                    models.Chunk.tenant_id == self._tenant_id,
                    models.Chunk.id == chunk_id,
                    models.Document.tenant_id == self._tenant_id,
                )
            )
        ).one_or_none()
        if source is None:
            raise ValueError("citation source chunk is not in the repository tenant")
        source_chunk, source_document = source
        is_media = source_document.kind in {DocumentKind.AUDIO.value, DocumentKind.VIDEO.value}
        has_media_metadata = any(
            value is not None
            for value in (
                time_start_ms,
                time_end_ms,
                transcript_segment_id,
                speaker_id,
                speaker_name,
            )
        )
        if is_media and time_start_ms is None:
            raise ValueError("media citations require a timestamp span")
        if not is_media and has_media_metadata:
            raise ValueError("ordinary document citations cannot carry media metadata")
        if time_start_ms is not None and time_end_ms is not None:
            if time_start_ms < 0 or time_end_ms <= time_start_ms:
                raise ValueError("citation timestamp span is invalid")
            if source_document.duration_ms is None or time_end_ms > source_document.duration_ms:
                raise ValueError("citation timestamp exceeds the media duration")
            if (
                source_chunk.time_start_ms is None
                or source_chunk.time_end_ms is None
                or time_start_ms < source_chunk.time_start_ms
                or time_end_ms > source_chunk.time_end_ms
            ):
                raise ValueError("citation timestamp is outside its source chunk")
        if (
            transcript_segment_id is not None
            and transcript_segment_id != source_chunk.transcript_segment_id
        ):
            raise ValueError("citation transcript segment does not match its source chunk")
        if transcript_segment_id is not None:
            segment = (
                await self._session.execute(
                    select(models.TranscriptSegment).where(
                        models.TranscriptSegment.tenant_id == self._tenant_id,
                        models.TranscriptSegment.id == transcript_segment_id,
                        models.TranscriptSegment.document_id == source_document.id,
                    )
                )
            ).scalar_one_or_none()
            if segment is None:
                raise ValueError("citation transcript segment does not belong to the source")
            if (
                time_start_ms is not None
                and time_end_ms is not None
                and (time_start_ms < segment.start_ms or time_end_ms > segment.end_ms)
            ):
                raise ValueError("citation timestamp is outside its transcript segment")
            if speaker_id is not None and speaker_id != segment.speaker_id:
                raise ValueError("citation speaker does not match its transcript segment")
        if speaker_id is not None and speaker_id != source_chunk.speaker_id:
            raise ValueError("citation speaker does not match its source chunk")
        if speaker_name is not None and speaker_name != source_chunk.speaker_name:
            raise ValueError("citation speaker name does not match its source chunk")
        row = models.Citation(
            tenant_id=self._tenant_id,
            message_id=message_id,
            chunk_id=chunk_id,
            char_start=char_start,
            char_end=char_end,
            score=score,
            time_start_ms=time_start_ms,
            time_end_ms=time_end_ms,
            transcript_segment_id=transcript_segment_id,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
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

    async def list_for_messages_hydrated_batch(self, message_ids: list[UUID]) -> list[CitationView]:
        """Hydrated citations for MANY messages in ONE query (#446 round-2).

        The summarizer's capture path: a backlog batch must not issue one
        citation query per covered message. Same shape/joins as
        :meth:`list_for_message_hydrated`; ordering is by message then span.
        """
        if not message_ids:
            return []
        stmt = (
            select(
                models.Citation.id,
                models.Citation.message_id,
                models.Citation.chunk_id,
                models.Citation.char_start,
                models.Citation.char_end,
                models.Citation.score,
                models.Citation.time_start_ms,
                models.Citation.time_end_ms,
                models.Citation.transcript_segment_id,
                models.Citation.speaker_id,
                models.Citation.speaker_name,
                models.Chunk.text,
                models.Document.id,
                models.Document.filename,
            )
            .join(models.Chunk, models.Chunk.id == models.Citation.chunk_id)
            .join(models.Document, models.Document.id == models.Chunk.document_id)
            .where(
                models.Citation.tenant_id == self._tenant_id,
                models.Citation.message_id.in_(message_ids),
                models.Chunk.tenant_id == self._tenant_id,
                models.Document.tenant_id == self._tenant_id,
            )
            .order_by(models.Citation.message_id, models.Citation.char_start.asc())
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
                time_start_ms=row[6],
                time_end_ms=row[7],
                transcript_segment_id=row[8],
                speaker_id=row[9],
                speaker_name=row[10],
                snippet=row[11],
                document_id=row[12],
                document_name=row[13],
            )
            for row in rows
        ]

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
                models.Citation.time_start_ms,
                models.Citation.time_end_ms,
                models.Citation.transcript_segment_id,
                models.Citation.speaker_id,
                models.Citation.speaker_name,
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
                time_start_ms=row[6],
                time_end_ms=row[7],
                transcript_segment_id=row[8],
                speaker_id=row[9],
                speaker_name=row[10],
                snippet=row[11],
                document_id=row[12],
                document_name=row[13],
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
                models.Citation.time_start_ms,
                models.Citation.time_end_ms,
                models.Citation.transcript_segment_id,
                models.Citation.speaker_id,
                models.Citation.speaker_name,
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
                    time_start_ms=row[6],
                    time_end_ms=row[7],
                    transcript_segment_id=row[8],
                    speaker_id=row[9],
                    speaker_name=row[10],
                    snippet=row[11],
                    document_id=row[12],
                    document_name=row[13],
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
        self, principal_id: UUID, *, group_ids: frozenset[UUID] = frozenset()
    ) -> tuple[frozenset[UUID], frozenset[UUID]]:
        """The ``(document_ids, collection_ids)`` granted to this requester.

        The resolved-id-set form of the SQL grant ``EXISTS`` (ADR-0010 §4): the
        retrieval chokepoint resolves the requester's grants per request and
        folds them into the engine's :class:`~app.search.filters.SearchAllowFilter`.
        Tenant-scoped (INV-1 — a cross-tenant grant never widens the filter). A
        revoked grant (row deleted) vanishes from the sets — deny-by-default
        restored.

        Covers both of the requester's principal kinds so the engine mirror and
        ``retrieval.queries._grant_exists`` admit exactly the same rows: their
        ``user`` principal, plus every ``group`` principal in ``group_ids``
        (ADR-0022 §5). ``group_ids`` defaults to empty, which emits no group
        term at all — the pre-ADR-0022 behaviour, and the fail-closed default.
        """
        principal_match = [
            and_(
                models.Grant.principal_type == GrantPrincipalType.USER.value,
                models.Grant.principal_id == principal_id,
            )
        ]
        if group_ids:
            principal_match.append(
                and_(
                    models.Grant.principal_type == GrantPrincipalType.GROUP.value,
                    models.Grant.principal_id.in_(group_ids),
                )
            )
        stmt = select(models.Grant.resource_type, models.Grant.resource_id).where(
            models.Grant.tenant_id == self._tenant_id,
            or_(*principal_match),
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


class GroupRepository(_TenantScopedRepository):
    """Groups and their membership within one tenant (ADR-0022, INV-1).

    The persistence seam behind ``group`` grants. Every method is tenant-scoped,
    so a group minted in tenant A is invisible to a tenant-B repository and can
    never widen that tenant's allow-set (asserted by the negative tests).
    Authorization (admin-only management) lives one layer up in
    :class:`~app.services.groups_service.GroupsService`. Writes are flushed, not
    committed — the caller owns the transaction boundary.
    """

    async def create(
        self, *, name: str, created_by: UUID | None, kind: GroupKind = GroupKind.USER
    ) -> Group:
        """Persist a group. Raises ``IntegrityError`` on a duplicate name."""
        row = models.Group(
            tenant_id=self._tenant_id,
            name=name.strip(),
            kind=kind.value,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_group(row)

    async def get(self, group_id: UUID) -> Group | None:
        """One group by id, tenant-scoped (a foreign tenant's id reads as ``None``)."""
        row = await self._get_row(group_id)
        return None if row is None else _to_group(row)

    async def _get_row(self, group_id: UUID) -> models.Group | None:
        stmt = select(models.Group).where(
            models.Group.tenant_id == self._tenant_id,
            models.Group.id == group_id,
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def get_by_name(self, name: str) -> Group | None:
        """One group by case-insensitive name (mirrors the unique index)."""
        stmt = select(models.Group).where(
            models.Group.tenant_id == self._tenant_id,
            func.lower(models.Group.name) == name.strip().lower(),
        )
        row = (await self._session.execute(stmt)).scalars().one_or_none()
        return None if row is None else _to_group(row)

    async def list_all(self) -> list[Group]:
        """Every group in the tenant, system group first, then by name."""
        stmt = select(models.Group).where(models.Group.tenant_id == self._tenant_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return sorted(
            (_to_group(r) for r in rows),
            key=lambda g: (g.kind is not GroupKind.SYSTEM, g.name.lower()),
        )

    async def rename(self, group_id: UUID, *, name: str) -> Group | None:
        """Rename a group; ``None`` if it does not exist in this tenant."""
        row = await self._get_row(group_id)
        if row is None:
            return None
        row.name = name.strip()
        await self._session.flush()
        # ``updated_at`` carries ``onupdate``, so the UPDATE expires it; refresh
        # before mapping or reading it would attempt lazy IO outside the async
        # greenlet (the same pattern the other updating repositories use).
        await self._session.refresh(row)
        return _to_group(row)

    async def delete(self, group_id: UUID) -> bool:
        """Delete a group with its membership and its grants; ``False`` if absent.

        Both cleanups are **explicit rather than delegated to the database**, and
        for different reasons:

        * ``grants.principal_id`` carries **no FK by design** (the principal
          namespace spans tables — spec 0004 §2.2), so nothing in the schema
          would ever remove a grant naming this group. Left behind it is a
          dangling row that keeps claiming a dead principal; deleting it here is
          what makes ADR-0022 §7's "deleting the group cascades its grants" true.
        * ``group_members`` *does* have an ``ON DELETE CASCADE``, but relying on
          it alone would make the outcome depend on the engine enforcing foreign
          keys (SQLite does not, unless ``PRAGMA foreign_keys=ON``). Deleting the
          rows explicitly makes the behaviour identical everywhere it runs.
        """
        row = await self._get_row(group_id)
        if row is None:
            return False
        await self._session.execute(
            delete(models.GroupMember).where(
                models.GroupMember.tenant_id == self._tenant_id,
                models.GroupMember.group_id == group_id,
            )
        )
        await self._session.execute(
            delete(models.Grant).where(
                models.Grant.tenant_id == self._tenant_id,
                models.Grant.principal_type == GrantPrincipalType.GROUP.value,
                models.Grant.principal_id == group_id,
            )
        )
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def ensure_system_group(self) -> tuple[Group, bool]:
        """Get-or-create the tenant's derived "All members" group (ADR-0022 §3).

        Returns ``(group, created_here)``. ``created_here`` is True only for the
        transaction that actually inserted, so the caller can audit the creation
        exactly once (INV-6) without a concurrent loser double-recording it.

        Idempotent: a partial unique index allows at most one per tenant, so a
        concurrent create loses the race and re-reads. The group is created
        lazily on first use rather than at tenant provisioning, so existing
        tenants need no back-fill.
        """
        stmt = select(models.Group).where(
            models.Group.tenant_id == self._tenant_id,
            models.Group.kind == GroupKind.SYSTEM.value,
        )
        row = (await self._session.execute(stmt)).scalars().one_or_none()
        if row is not None:
            return _to_group(row), False

        # Insert conflict-ignoring, then re-read. A plain INSERT would abort the
        # whole transaction on the partial unique index when two first-time
        # requests for the same tenant race, surfacing as a 500 on an ordinary
        # GET. DO NOTHING lets the loser fall through to the winner's row.
        values = {
            "id": uuid_mod.uuid4(),
            "tenant_id": self._tenant_id,
            "name": SYSTEM_GROUP_NAME,
            "kind": GroupKind.SYSTEM.value,
            "created_by": None,
        }
        dialect = self._session.bind.dialect.name if self._session.bind is not None else ""
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        result = await self._session.execute(
            insert(models.Group).values(**values).on_conflict_do_nothing()
        )
        await self._session.flush()
        created = bool(result.rowcount)
        return _to_group((await self._session.execute(stmt)).scalars().one()), created

    async def add_member(self, *, group_id: UUID, user_id: UUID, added_by: UUID | None) -> bool:
        """Add a user to a group. Idempotent — ``False`` if already a member."""
        # Conflict-ignoring insert rather than check-then-add: two concurrent
        # "add the same user" requests would both see no row, and the loser's
        # unique violation on (group_id, user_id) would abort the transaction —
        # a 500 where the contract promises an idempotent 204. The rowcount says
        # whether THIS request inserted, so only the winner emits the audit.
        values = {
            "id": uuid_mod.uuid4(),
            "tenant_id": self._tenant_id,
            "group_id": group_id,
            "user_id": user_id,
            "added_by": added_by,
        }
        dialect = self._session.bind.dialect.name if self._session.bind is not None else ""
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        result = await self._session.execute(
            insert(models.GroupMember)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["group_id", "user_id"])
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def remove_member(self, *, group_id: UUID, user_id: UUID) -> bool:
        """Remove a user from a group; ``False`` if they were not a member."""
        stmt = select(models.GroupMember).where(
            models.GroupMember.tenant_id == self._tenant_id,
            models.GroupMember.group_id == group_id,
            models.GroupMember.user_id == user_id,
        )
        row = (await self._session.execute(stmt)).scalars().one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def member_counts(self) -> dict[UUID, int]:
        """Explicit member counts for every group in the tenant, in ONE query.

        The listing path needs a count per group; doing that per group is a
        classic N+1 (and each call would re-read the group too). Groups with no
        members are simply absent from the mapping — the caller defaults them to
        zero. The system group never appears: it has no membership rows at all.
        """
        stmt = (
            select(models.GroupMember.group_id, func.count())
            .where(models.GroupMember.tenant_id == self._tenant_id)
            .group_by(models.GroupMember.group_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return dict(rows)  # type: ignore[arg-type]

    async def list_member_ids(self, group_id: UUID) -> list[UUID]:
        """The user ids explicitly in ``group_id`` (empty for the system group)."""
        stmt = select(models.GroupMember.user_id).where(
            models.GroupMember.tenant_id == self._tenant_id,
            models.GroupMember.group_id == group_id,
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_members(self, group_id: UUID) -> list[User]:
        """The group's members as users, ordered by email — ONE joined query.

        Resolving ids then fetching each user is an unbounded N+1 on an
        unpaginated endpoint; a group with thousands of members would issue
        thousands of sequential reads. Tenant-scoped on both sides (INV-1).
        """
        stmt = (
            select(models.User)
            .join(models.GroupMember, models.GroupMember.user_id == models.User.id)
            .where(
                models.GroupMember.tenant_id == self._tenant_id,
                models.GroupMember.group_id == group_id,
                models.User.tenant_id == self._tenant_id,
            )
            .order_by(func.lower(models.User.email))
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_user(r) for r in rows]

    async def group_ids_for_user(self, user_id: UUID) -> frozenset[UUID]:
        """The group principals ``user_id`` carries — the allow-set hot path.

        Returns the user's **explicit** memberships plus the tenant's system
        group id when one exists (ADR-0022 §3: tenant-wide membership is derived,
        so there are no rows to read for it). Read once per request and never
        cached on the principal or in the token — that is precisely what makes a
        removal take effect on the next request (ADR-0022 §7).

        Returns an empty set for a user with no groups; an empty set narrows the
        allow-set to ownership plus user grants, i.e. it fails closed.
        """
        stmt = select(models.GroupMember.group_id).where(
            models.GroupMember.tenant_id == self._tenant_id,
            models.GroupMember.user_id == user_id,
        )
        explicit = set((await self._session.execute(stmt)).scalars().all())
        system = await self._session.execute(
            select(models.Group.id).where(
                models.Group.tenant_id == self._tenant_id,
                models.Group.kind == GroupKind.SYSTEM.value,
            )
        )
        system_id = system.scalars().one_or_none()
        if system_id is not None:
            explicit.add(system_id)
        return frozenset(explicit)


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

    async def rotate_value(
        self,
        secret_id: UUID,
        *,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
        hint: str,
    ) -> Secret | None:
        """Rotate a secret's value **in place by id** (tenant-scoped, INV-1).

        The by-reference rotation ADR-0019 §1 needs: a bound consumer (a source
        row holding ``auth_secret_ref``) replaces the credential under the SAME
        row regardless of which admin originally stored it — the handle stays
        stable, no per-owner duplicate is minted. One atomic
        ``UPDATE … RETURNING`` (no read-then-write): a row deleted by a racing
        transaction after the caller's authorization read simply matches
        nothing here, and the ``None`` return is the caller's race signal.
        """
        stmt = (
            update(models.Secret)
            .where(
                models.Secret.tenant_id == self._tenant_id,
                models.Secret.id == secret_id,
            )
            .values(
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=key_version,
                hint=hint,
            )
            .returning(models.Secret)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        await self._session.flush()
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


class McpServerRepository(_TenantScopedRepository):
    """Registered remote MCP servers within one tenant (ADR-0012 §5, issue #226).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``server_id``
    resolves to ``None``/no rows, so the existence-non-disclosure 404 is enforced
    one layer up off the ``None`` return. Ownership (owner-or-admin) is layered in
    :class:`~app.services.mcp_servers_service.McpServersService`. Rows never hold
    the credential — only the CC-C ``auth_secret_ref`` + a masked ``secret_hint``.
    Writes are flushed not committed; the caller owns the transaction boundary
    (audits atomically with the write).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        name: str,
        transport: str,
        endpoint_url: str,
        auth_secret_ref: UUID | None,
        secret_hint: str | None,
        status: McpServerStatus = McpServerStatus.PENDING,
    ) -> McpServer:
        row = models.McpServer(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            transport=transport,
            endpoint_url=endpoint_url,
            auth_secret_ref=auth_secret_ref,
            secret_hint=secret_hint,
            enabled=True,
            status=status.value,
            discovered_tools=[],
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_mcp_server(row)

    async def get(self, server_id: UUID) -> McpServer | None:
        row = await self._get_row(server_id)
        return _to_mcp_server(row) if row is not None else None

    async def _get_row(self, server_id: UUID) -> models.McpServer | None:
        stmt = select(models.McpServer).where(
            models.McpServer.tenant_id == self._tenant_id,
            models.McpServer.id == server_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_enabled_for_owner(self, owner_id: UUID) -> list[McpServer]:
        """Every **enabled** server an owner registered in this tenant (INV-1/INV-2).

        The read the tool bridge (#227) uses to resolve a run's MCP tools: tenant-
        *and* owner-scoped and filtered to ``enabled`` in the SQL, so a disabled
        server — or a foreign-tenant / non-owned one — never even loads, and thus
        can never be offered or invoked in a chat run (the deny-by-default cross-
        tenant/disabled fence lives here + one more time in the bridge). Ordered by
        ``(created_at, id)`` for a stable resolution order.
        """
        stmt = (
            select(models.McpServer)
            .where(
                models.McpServer.tenant_id == self._tenant_id,
                models.McpServer.owner_id == owner_id,
                models.McpServer.enabled.is_(True),
            )
            .order_by(models.McpServer.created_at.asc(), models.McpServer.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_mcp_server(r) for r in rows]

    async def list_for_owner_page(
        self,
        owner_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[McpServer]:
        """A keyset page of an owner's MCP servers (newest first).

        Owner- *and* tenant-scoped (deny-by-default ownership, spec 0004 §2.2 +
        INV-1). Ordered by ``(created_at, id)`` descending with ``id`` the stable
        tiebreaker (mirrors the sources/documents keyset).
        """
        conditions = [
            models.McpServer.tenant_id == self._tenant_id,
            models.McpServer.owner_id == owner_id,
        ]
        if after_id is not None:
            boundary_created_at = (
                select(models.McpServer.created_at)
                .where(
                    models.McpServer.tenant_id == self._tenant_id,
                    models.McpServer.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.McpServer.created_at < boundary_created_at,
                    and_(
                        models.McpServer.created_at == boundary_created_at,
                        models.McpServer.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.McpServer)
            .where(*conditions)
            .order_by(models.McpServer.created_at.desc(), models.McpServer.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_mcp_server(r) for r in rows]

    async def update(
        self,
        server_id: UUID,
        *,
        name: str | None = None,
        endpoint_url: str | None = None,
        enabled: bool | None = None,
        auth_secret_ref: UUID | None = None,
        secret_hint: str | None = None,
        clear_auth: bool = False,
    ) -> McpServer | None:
        """Apply a partial update to one server (tenant-scoped), or ``None``.

        Only the passed fields change. ``auth_secret_ref``/``secret_hint`` are
        applied together on a credential rotation; ``clear_auth=True`` nulls both
        (the caller having already deleted the vault secret). Ownership is enforced
        one layer up; a foreign-tenant id matches nothing here (INV-1).
        """
        row = await self._get_row(server_id)
        if row is None:
            return None
        if name is not None:
            row.name = name
        if endpoint_url is not None:
            row.endpoint_url = endpoint_url
        if enabled is not None:
            row.enabled = enabled
        if clear_auth:
            row.auth_secret_ref = None
            row.secret_hint = None
        elif auth_secret_ref is not None:
            row.auth_secret_ref = auth_secret_ref
            row.secret_hint = secret_hint
        await self._session.flush()
        await self._session.refresh(row)
        return _to_mcp_server(row)

    async def update_health(
        self,
        server_id: UUID,
        *,
        status: McpServerStatus,
        last_health_at: datetime | None,
        last_error: str | None,
        discovered_tools: list[dict[str, object]],
    ) -> McpServer | None:
        """Persist a probe outcome: status + health timestamp + tool snapshot.

        On a healthy probe the caller passes ``status=ready`` with the fresh
        ``last_health_at`` + discovered tools; on a failure ``status=error`` with a
        safe ``last_error`` (the previous tool snapshot is left in place by passing
        it back). Tenant-scoped; ``None`` for a foreign id.
        """
        row = await self._get_row(server_id)
        if row is None:
            return None
        row.status = status.value
        row.last_health_at = last_health_at
        row.last_error = last_error
        row.discovered_tools = discovered_tools
        await self._session.flush()
        await self._session.refresh(row)
        return _to_mcp_server(row)

    async def delete(self, server_id: UUID) -> bool:
        """Delete a server by id (tenant-scoped); ``True`` if one was removed.

        Idempotent — ``False`` if no such server exists in this tenant. Ownership
        is enforced one layer up; a foreign-tenant id matches nothing (INV-1).
        """
        row = await self._get_row(server_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


class LlmProviderRepository(_TenantScopedRepository):
    """Registered per-tenant LLM providers within one tenant (foundation PR).

    Mirrors :class:`McpServerRepository`. Tenant-scoped like every repository
    (INV-1): a foreign-tenant ``provider_id`` resolves to ``None``/no rows, so the
    existence-non-disclosure 404 is enforced one layer up off the ``None`` return.
    Admin-gating is enforced by the ``/admin`` router; ownership is layered in
    :class:`~app.services.llm_providers_service.LlmProviderService`. Rows never hold
    the API key — only the CC-C ``api_key_secret_ref`` + a masked ``secret_hint``.
    Writes are flushed not committed; the caller owns the transaction boundary
    (audits atomically with the write).
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        name: str,
        provider_type: str,
        base_url: str,
        api_key_secret_ref: UUID | None,
        secret_hint: str | None,
        status: LlmProviderStatus = LlmProviderStatus.PENDING,
    ) -> LlmProvider:
        row = models.LlmProvider(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key_secret_ref=api_key_secret_ref,
            secret_hint=secret_hint,
            enabled=True,
            status=status.value,
            discovered_models=[],
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_llm_provider(row)

    async def get(self, provider_id: UUID) -> LlmProvider | None:
        row = await self._get_row(provider_id)
        return _to_llm_provider(row) if row is not None else None

    async def _get_row(self, provider_id: UUID) -> models.LlmProvider | None:
        stmt = select(models.LlmProvider).where(
            models.LlmProvider.tenant_id == self._tenant_id,
            models.LlmProvider.id == provider_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_tenant(self) -> list[LlmProvider]:
        """Every registered provider in this tenant (newest first, INV-1).

        Tenant-scoped, not owner-scoped: an LLM provider is tenant-wide admin config
        (any tenant admin manages any provider in the tenant), unlike an MCP server
        which is per-owner. Ordered by ``(created_at, id)`` descending for a stable
        newest-first list.
        """
        stmt = (
            select(models.LlmProvider)
            .where(models.LlmProvider.tenant_id == self._tenant_id)
            .order_by(models.LlmProvider.created_at.desc(), models.LlmProvider.id.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_llm_provider(r) for r in rows]

    async def update(
        self,
        provider_id: UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        enabled: bool | None = None,
        api_key_secret_ref: UUID | None = None,
        secret_hint: str | None = None,
        clear_api_key: bool = False,
    ) -> LlmProvider | None:
        """Apply a partial update to one provider (tenant-scoped), or ``None``.

        Only the passed fields change. ``api_key_secret_ref``/``secret_hint`` are
        applied together on a key rotation; ``clear_api_key=True`` nulls both (the
        caller having already deleted the vault secret). Admin-gating is enforced by
        the router; a foreign-tenant id matches nothing here (INV-1).
        """
        row = await self._get_row(provider_id)
        if row is None:
            return None
        if name is not None:
            row.name = name
        if base_url is not None:
            row.base_url = base_url
        if enabled is not None:
            row.enabled = enabled
        if clear_api_key:
            row.api_key_secret_ref = None
            row.secret_hint = None
        elif api_key_secret_ref is not None:
            row.api_key_secret_ref = api_key_secret_ref
            row.secret_hint = secret_hint
        await self._session.flush()
        await self._session.refresh(row)
        return _to_llm_provider(row)

    async def set_discovery(
        self,
        provider_id: UUID,
        *,
        status: LlmProviderStatus,
        discovered_models: list[dict[str, object]],
        last_error: str | None,
        last_discovery_at: datetime | None,
    ) -> LlmProvider | None:
        """Persist a discovery outcome: status + discovery timestamp + model snapshot.

        On a successful discovery the caller passes ``status=ready`` with the fresh
        ``last_discovery_at`` + discovered models; on a failure ``status=error`` with
        a safe ``last_error`` (the previous model snapshot is left in place by passing
        it back). Tenant-scoped; ``None`` for a foreign id.
        """
        row = await self._get_row(provider_id)
        if row is None:
            return None
        row.status = status.value
        row.last_discovery_at = last_discovery_at
        row.last_error = last_error
        row.discovered_models = discovered_models
        await self._session.flush()
        await self._session.refresh(row)
        return _to_llm_provider(row)

    async def delete(self, provider_id: UUID) -> bool:
        """Delete a provider by id (tenant-scoped); ``True`` if one was removed.

        Idempotent — ``False`` if no such provider exists in this tenant. A
        foreign-tenant id matches nothing (INV-1).
        """
        row = await self._get_row(provider_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True


class TenantToolPolicyRepository(_TenantScopedRepository):
    """Per-tenant tool-governance overrides within one tenant (issue #223).

    Tenant-scoped like every repository (INV-1): every query filters on
    ``tenant_id``, so one tenant can never read or write another's policy. A row's
    absence is meaningful — it means "use the tool's built-in default" (the
    deny-by-default rule the service and the approval gate enforce), so this
    exposes a plain ``get_by_tool`` returning ``None`` rather than fabricating a
    default. Writes are flushed not committed; the caller owns the transaction
    boundary (so the audit event commits atomically with the write).
    """

    async def list_all(self) -> list[TenantToolPolicy]:
        """Every stored per-tool override for this tenant (stable order by tool name)."""
        stmt = (
            select(models.TenantToolPolicy)
            .where(models.TenantToolPolicy.tenant_id == self._tenant_id)
            .order_by(models.TenantToolPolicy.tool_name.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_tenant_tool_policy(r) for r in rows]

    async def get_by_tool(self, tool_name: str) -> TenantToolPolicy | None:
        """The tenant's override for ``tool_name``, or ``None`` if none is stored.

        ``None`` is not an error — it means the tool's built-in default is in force
        (deny-by-default for a ``requires_approval`` tool). Tenant-scoped (INV-1).
        """
        row = await self._get_row(tool_name)
        return _to_tenant_tool_policy(row) if row is not None else None

    async def _get_row(self, tool_name: str) -> models.TenantToolPolicy | None:
        stmt = select(models.TenantToolPolicy).where(
            models.TenantToolPolicy.tenant_id == self._tenant_id,
            models.TenantToolPolicy.tool_name == tool_name,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        tool_name: str,
        enabled: bool,
        requires_approval: bool,
        updated_by: UUID | None,
    ) -> TenantToolPolicy:
        """Create or update the tenant's override for ``tool_name`` (a per-tenant upsert).

        The ``(tenant_id, tool_name)`` unique constraint makes this a singleton per
        tool per tenant: an existing row is updated in place, otherwise a new one is
        inserted. Tenant-scoped (INV-1) — the write is always keyed to this
        repository's tenant, never request input. Flushed, not committed.
        """
        row = await self._get_row(tool_name)
        if row is None:
            row = models.TenantToolPolicy(
                tenant_id=self._tenant_id,
                tool_name=tool_name,
                enabled=enabled,
                requires_approval=requires_approval,
                updated_by=updated_by,
            )
            self._session.add(row)
        else:
            row.enabled = enabled
            row.requires_approval = requires_approval
            row.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(row)
        return _to_tenant_tool_policy(row)


class TenantSandboxPolicyRepository(_TenantScopedRepository):
    """The per-tenant code-execution sandbox policy within one tenant (issue #233).

    Tenant-scoped like every repository (INV-1): every query filters on ``tenant_id``,
    so one tenant can never read or write another's policy. A row's absence is
    meaningful — it means "code execution is DISABLED for the tenant" (the
    deny-by-default rule the service and the enforcement path enforce), so this exposes
    a plain ``get`` returning ``None`` rather than fabricating a default. Writes are
    flushed not committed; the caller owns the transaction boundary (so the audit event
    commits atomically with the write).
    """

    async def get(self) -> TenantSandboxPolicy | None:
        """The tenant's sandbox policy, or ``None`` if none is stored (deny-by-default).

        ``None`` is not an error — it means code execution is disabled for the tenant
        (the deny-by-default rule). Tenant-scoped (INV-1).
        """
        row = await self._get_row()
        return _to_tenant_sandbox_policy(row) if row is not None else None

    async def _get_row(self) -> models.TenantSandboxPolicy | None:
        stmt = select(models.TenantSandboxPolicy).where(
            models.TenantSandboxPolicy.tenant_id == self._tenant_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        enabled: bool,
        allowed_packages: tuple[str, ...],
        denied_packages: tuple[str, ...],
        egress_allowed: bool,
        egress_allowlist: tuple[str, ...],
        max_runtime_s: int,
        max_memory_mb: int,
        daily_runtime_cap_s: int,
        max_concurrency: int,
        updated_by: UUID | None,
    ) -> TenantSandboxPolicy:
        """Create or update the tenant's sandbox policy (a per-tenant singleton upsert).

        The ``(tenant_id)`` unique constraint makes this a singleton per tenant: an
        existing row is updated in place, otherwise a new one is inserted. Tenant-scoped
        (INV-1) — the write is always keyed to this repository's tenant, never request
        input. Flushed, not committed. The caller (service) is responsible for having
        already clamped the caps + stripped the metadata IP to the config ceiling.
        """
        row = await self._get_row()
        if row is None:
            row = models.TenantSandboxPolicy(
                tenant_id=self._tenant_id,
                enabled=enabled,
                allowed_packages=list(allowed_packages),
                denied_packages=list(denied_packages),
                egress_allowed=egress_allowed,
                egress_allowlist=list(egress_allowlist),
                max_runtime_s=max_runtime_s,
                max_memory_mb=max_memory_mb,
                daily_runtime_cap_s=daily_runtime_cap_s,
                max_concurrency=max_concurrency,
                updated_by=updated_by,
            )
            self._session.add(row)
        else:
            row.enabled = enabled
            row.allowed_packages = list(allowed_packages)
            row.denied_packages = list(denied_packages)
            row.egress_allowed = egress_allowed
            row.egress_allowlist = list(egress_allowlist)
            row.max_runtime_s = max_runtime_s
            row.max_memory_mb = max_memory_mb
            row.daily_runtime_cap_s = daily_runtime_cap_s
            row.max_concurrency = max_concurrency
            row.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(row)
        return _to_tenant_sandbox_policy(row)


class TenantAutonomyPolicyRepository(_TenantScopedRepository):
    """The per-tenant assistant autonomy cap within one tenant (issue #218).

    Tenant-scoped like every repository (INV-1): every query filters on ``tenant_id``,
    so one tenant can never read or write another's cap. A row's absence is meaningful
    — it means "no ceiling" (an assistant runs at its own configured level), so this
    exposes a plain ``get`` returning ``None`` rather than fabricating a default.
    Writes are flushed not committed; the caller owns the transaction boundary (so the
    audit event commits atomically with the write).
    """

    async def get(self) -> TenantAutonomyPolicy | None:
        """The tenant's autonomy cap, or ``None`` if none is stored (no ceiling).

        ``None`` is not an error — it means there is no per-tenant ceiling (the
        permissive default). Tenant-scoped (INV-1).
        """
        row = await self._get_row()
        return _to_tenant_autonomy_policy(row) if row is not None else None

    async def _get_row(self) -> models.TenantAutonomyPolicy | None:
        stmt = select(models.TenantAutonomyPolicy).where(
            models.TenantAutonomyPolicy.tenant_id == self._tenant_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        max_autonomy: AutonomyLevel,
        updated_by: UUID | None,
    ) -> TenantAutonomyPolicy:
        """Create or update the tenant's autonomy cap (a per-tenant singleton upsert).

        The ``(tenant_id)`` unique constraint makes this a singleton per tenant: an
        existing row is updated in place, otherwise a new one is inserted. Tenant-scoped
        (INV-1) — the write is always keyed to this repository's tenant, never request
        input. Flushed, not committed.
        """
        row = await self._get_row()
        if row is None:
            row = models.TenantAutonomyPolicy(
                tenant_id=self._tenant_id,
                max_autonomy=max_autonomy.value,
                updated_by=updated_by,
            )
            self._session.add(row)
        else:
            row.max_autonomy = max_autonomy.value
            row.updated_by = updated_by
        await self._session.flush()
        await self._session.refresh(row)
        return _to_tenant_autonomy_policy(row)


log = get_logger(__name__)


def _classify_source_ip(
    value: str | None,
) -> tuple[AuditSourceOrigin, str | None, str | None]:
    """Decide an event's origin from whatever the caller offered as ``source_ip``.

    Returns ``(origin, address_to_store, unrecognised_text)``. ``address_to_store`` is
    non-None **iff** the origin is ``client`` — the pair the table's CHECK constraint
    enforces — and ``unrecognised_text`` is non-None only when the caller sent
    something that is neither an address nor a sentinel we ship, which the caller
    should log.

    **Why the origin is computed here rather than by the caller.** An earlier revision
    returned ``(address, sentinel)`` and let ``record`` infer the origin from which was
    None. That could not express the third case: *no value at all*. ``None`` and ``""``
    both came back as ``(None, None)``, identical to a caller who had supplied a real
    address — so the writer labelled them ``client`` with a NULL address, which is
    exactly the pair the CHECK constraint forbids. Every ``/auth`` route reaches this
    path with a bare ``None`` when ``request.client`` is unset (uvicorn over a UNIX
    socket), so the audit insert would have aborted the login transaction it was
    recording: the very failure this whole change exists to remove, moved from Celery
    to the login endpoint. Returning the origin makes that state unrepresentable.

    No address given is ``unknown``, not ``system``: something asked for this, we just
    could not see from where. Only an explicit ``"system"`` sentinel means the platform
    acted with no client at all.

    **Python's parser is not Postgres's.** Two forms need care, both verified against
    the live database rather than assumed:

    * A ZONE-SCOPED address (``fe80::1%eth0``) parses happily in Python but
      ``select 'fe80::1%eth0'::inet`` errors — so storing the raw text would reproduce
      the exact rollback this function exists to prevent, for any link-local peer. The
      zone is stripped before the address is stored.
    * A BRACKETED or port-bearing form (``[2001:db8::1]:443``) is not an address to
      Python, so it would silently become NULL and lose real audit fidelity. It is
      unwrapped first, and only then parsed.
    """
    text = (value or "").strip()
    if not text:
        # Nothing was offered. `unknown` is the honest reading — and, unlike the
        # `client`/NULL pair this used to produce, one the constraint accepts.
        return AuditSourceOrigin.UNKNOWN, None, None

    candidate = text
    # `[v6]:port` / `[v6]` — unwrap before parsing so a real address is not lost.
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing > 0:
            candidate = candidate[1:closing]
    # A zone id is meaningful to the OS, not to `inet`; drop it rather than fail.
    zone_index = candidate.find("%")
    if zone_index >= 0:
        candidate = candidate[:zone_index]

    # Parse to VALIDATE, but store the candidate text rather than `str(parsed)`.
    # Python's canonical form is not Postgres's: `ipaddress` renders
    # `::ffff:1.2.3.4` as `::ffff:102:304`, while `select '::ffff:1.2.3.4'::inet`
    # keeps the dotted form. Normalising here would quietly rewrite the address an
    # operator sees in the audit trail into a different spelling than the database
    # itself would have stored. (Note `ip_address` PRESERVES a zone id, so the strip
    # above — not the parse — is what keeps link-local addresses out of `INET`.)
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        try:
            # `INET` also accepts CIDR (`10.0.0.0/8`) — a network, not an address.
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            lowered = text.lower()
            if lowered == AuditSourceOrigin.SYSTEM.value:
                return AuditSourceOrigin.SYSTEM, None, None
            if lowered == AuditSourceOrigin.UNKNOWN.value:
                return AuditSourceOrigin.UNKNOWN, None, None
            # Neither an address nor a sentinel we ship. Still recorded — an audit
            # write must never crash the action it records — but handed back for the
            # caller to log, because silently losing every address is how a
            # misconfigured proxy destroys audit fidelity without anyone noticing.
            return AuditSourceOrigin.UNKNOWN, None, text
    return AuditSourceOrigin.CLIENT, candidate, None


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
        # Every event records WHERE it came from (#546). The envelope has always
        # required a source, but a background task has no client address — so callers
        # passed a sentinel, `INET` rejected it, and because the emit rides the caller's
        # transaction that rejection rolled back the ACTION being recorded. It took the
        # rolling summariser with it: rows carried evidence (written in the answer
        # transaction) but never a summary or a coverage cursor, and
        # `session.summarized` was never once written. SQLite cannot catch that class at
        # all — its `String(45)` variant accepts any text — so the offline suite stayed
        # green while the feature was dead in every Postgres deployment.
        #
        # `source_origin` makes the contract stateable instead of exception-ridden:
        # every event says where it came from, and an address is recorded exactly when
        # there was a client to have one. A CHECK constraint enforces the pair, so the
        # invariant does not rest on this method alone.
        origin, stored_ip, unrecognised = _classify_source_ip(source_ip)
        if unrecognised is not None:
            log.warning(
                "audit.source_ip_not_an_address",
                action=action,
                resource_type=resource_type,
                value_length=len(unrecognised),
            )
        row = models.AuditEvent(
            source_origin=origin.value,
            tenant_id=self._tenant_id,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome.value,
            request_id=request_id,
            source_ip=stored_ip,
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
        result_summary: str | None = None,
        ordinal: int = 0,
    ) -> ToolInvocation:
        """Append one tool-invocation record for this tenant, returning it.

        ``result_summary`` is the handler-produced, user-safe result line (#377);
        it is bounded HERE (the single write chokepoint) so no caller can persist
        an unbounded string into the trace.
        """
        row = models.ToolInvocation(
            tenant_id=self._tenant_id,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            tool_name=tool_name,
            args_hash=args_hash,
            ok=ok,
            error=error,
            result_summary=(result_summary[:300] if result_summary else None),
            ordinal=ordinal,
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
            .order_by(
                models.ToolInvocation.created_at.asc(),
                models.ToolInvocation.ordinal.asc(),
                models.ToolInvocation.id.asc(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_tool_invocation(r) for r in rows]

    async def list_for_messages(
        self, message_ids: Sequence[UUID]
    ) -> dict[UUID, list[ToolInvocation]]:
        """The tool trace per assistant message, batched (no N+1), oldest first.

        Hydrates the contract ``Message.tool_invocations`` (#377) the same way
        citations are hydrated for a history page: one IN query over the page's
        message ids, tenant-scoped (INV-1). Ids with no trace are simply absent —
        the caller defaults to ``[]``.
        """
        if not message_ids:
            return {}
        stmt = (
            select(models.ToolInvocation)
            .where(
                models.ToolInvocation.tenant_id == self._tenant_id,
                models.ToolInvocation.message_id.in_(list(message_ids)),
            )
            .order_by(
                models.ToolInvocation.created_at.asc(),
                models.ToolInvocation.ordinal.asc(),
                models.ToolInvocation.id.asc(),
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        by_message: dict[UUID, list[ToolInvocation]] = {}
        for row in rows:
            if row.message_id is None:  # pragma: no cover — filtered by the IN clause
                continue
            by_message.setdefault(row.message_id, []).append(_to_tool_invocation(row))
        return by_message


def _to_llm_usage(row: models.LlmUsage) -> LlmUsageRecord:
    return LlmUsageRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        answer_id=row.answer_id,
        message_id=row.message_id,
        run_id=row.run_id,
        model=row.model,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        cached_prompt_tokens=row.cached_prompt_tokens,
        cache_write_tokens=row.cache_write_tokens,
        context_prompt_tokens=row.context_prompt_tokens,
        created_at=row.created_at,
    )


class LlmUsageRepository(_TenantScopedRepository):
    """Per-answer LLM token/cache accounting within one tenant (#409, ADR-0016 §2.6).

    One ``record`` per produced answer (chat and headless runs alike — both ride
    the shared runtime), summing the answer loop's turns. Tenant-scoped (INV-1):
    tenant A's spend is invisible to a tenant-B repository. A trace/analytics
    table like ``tool_invocations`` (ordinary access, no UPDATE/DELETE revoke);
    writes are flushed not committed — the runtime owns the transaction so the
    usage row lands atomically with the answer it accounts for. Reads here are
    the substrate consumers (#300 analytics, budgets) build on.
    """

    async def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cached_prompt_tokens: int = 0,
        cache_write_tokens: int = 0,
        context_prompt_tokens: int | None = None,
        session_id: UUID | None = None,
        answer_id: UUID | None = None,
        message_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> LlmUsageRecord:
        """Append one usage record for this tenant, returning it.

        Values are clamped non-negative HERE (the single write chokepoint) so no
        caller can persist a negative count (the table CHECK is the backstop).
        """
        row = models.LlmUsage(
            tenant_id=self._tenant_id,
            session_id=session_id,
            answer_id=answer_id,
            message_id=message_id,
            run_id=run_id,
            model=model,
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            total_tokens=max(0, total_tokens),
            cached_prompt_tokens=max(0, cached_prompt_tokens),
            cache_write_tokens=max(0, cache_write_tokens),
            context_prompt_tokens=(
                max(0, context_prompt_tokens) if context_prompt_tokens is not None else None
            ),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_llm_usage(row)

    async def record_suggestion_usage(
        self,
        *,
        model: str,
        session_id: UUID,
        answer_id: UUID,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cached_prompt_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> bool:
        """Record the post-terminal follow-up-suggestions spend as its OWN usage scope.

        ADR-0016 §2.6 actual-model attribution: since #490 the suggestions nicety can
        run on a **dedicated** model, so its tokens are attributed to THAT model on
        their own row — never folded onto the ANSWER model's row (which would
        misattribute the spend to a model that did not incur it). The row is
        message-less (the nicety produced no assistant message) but grouped by
        ``session_id`` + ``answer_id`` so the spend stays accounted
        (:meth:`totals_for_session` sums it) and queryable, while
        ``COUNT(message_id)`` still excludes it — a suggestions call is not an extra
        "answer".

        **Idempotent** (R2-3 / #409): ``run_id == answer_id`` tags the suggestions
        scope — a tag the answer's base row and its #413 failover/salvage scopes (all
        ``run_id`` NULL) never carry. The spend is recorded at most ONCE per answer, so
        a repeated post-terminal application (a retry, a double-call) is a no-op and
        genuinely-billed tokens land exactly once. The post-terminal nicety is a single
        producer per answer, so this check-then-insert has no concurrent writer to
        race. Deltas are clamped non-negative by :meth:`record`. Tenant-scoped (INV-1).

        Returns whether a new row was written (``False`` ⇒ already recorded).
        """
        existing = await self._session.execute(
            select(models.LlmUsage.id)
            .where(
                models.LlmUsage.tenant_id == self._tenant_id,
                models.LlmUsage.answer_id == answer_id,
                models.LlmUsage.run_id == answer_id,
            )
            .limit(1)
        )
        if existing.first() is not None:
            return False
        await self.record(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cache_write_tokens=cache_write_tokens,
            session_id=session_id,
            answer_id=answer_id,
            message_id=None,
            run_id=answer_id,
        )
        return True

    async def list_for_session(self, session_id: UUID, *, limit: int = 200) -> list[LlmUsageRecord]:
        """The usage records for one chat session (tenant-scoped), oldest first."""
        stmt = (
            select(models.LlmUsage)
            .where(
                models.LlmUsage.tenant_id == self._tenant_id,
                models.LlmUsage.session_id == session_id,
            )
            .order_by(models.LlmUsage.created_at.asc(), models.LlmUsage.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_llm_usage(r) for r in rows]

    async def totals_for_session(self, session_id: UUID) -> LlmUsageTotals:
        """Summed accounting for one session (spec 0007 #429) — one GROUP-less SUM.

        Tenant-scoped (INV-1). A session with no usage rows yields all-zero
        totals (an empty meter, not an error). Computed in SQL so the read stays
        O(1) rows regardless of conversation length.
        """
        stmt = select(
            # COUNT(message_id) skips NULLs: answers = PRODUCED answers (the
            # contract's wording) — message-less route scopes (#413 failovers,
            # error-path salvage) contribute to the token sums but are not
            # extra "answers".
            func.count(models.LlmUsage.message_id),
            func.coalesce(func.sum(models.LlmUsage.prompt_tokens), 0),
            func.coalesce(func.sum(models.LlmUsage.completion_tokens), 0),
            func.coalesce(func.sum(models.LlmUsage.total_tokens), 0),
            func.coalesce(func.sum(models.LlmUsage.cached_prompt_tokens), 0),
            func.coalesce(func.sum(models.LlmUsage.cache_write_tokens), 0),
        ).where(
            models.LlmUsage.tenant_id == self._tenant_id,
            models.LlmUsage.session_id == session_id,
        )
        row = (await self._session.execute(stmt)).one()
        return LlmUsageTotals(
            answers=int(row[0]),
            prompt_tokens=int(row[1]),
            completion_tokens=int(row[2]),
            total_tokens=int(row[3]),
            cached_prompt_tokens=int(row[4]),
            cache_write_tokens=int(row[5]),
        )

    async def last_for_session(self, session_id: UUID) -> LlmUsageRecord | None:
        """The most recent usage record for one session, or ``None`` (spec 0007).

        The "how full was the window last turn" input of the context meter.
        """
        stmt = (
            select(models.LlmUsage)
            .where(
                models.LlmUsage.tenant_id == self._tenant_id,
                models.LlmUsage.session_id == session_id,
                # Only the message-bearing (winning) scope describes "the most
                # recent answer" — a failed/superseded route scope (#413) must
                # never win this read (its window occupancy is meaningless).
                models.LlmUsage.message_id.is_not(None),
            )
            .order_by(models.LlmUsage.created_at.desc(), models.LlmUsage.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_llm_usage(row) if row is not None else None


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
            elif key == "certification_state" and isinstance(value, CertificationState):
                row.certification_state = value.value
            else:
                setattr(row, key, value)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_assistant(row)

    async def list_for_tenant_page(
        self,
        *,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[Assistant]:
        """A keyset page of ALL the tenant's assistants (newest first) — admin governance.

        Tenant-scoped only (INV-1): unlike ``list_for_owner_page`` this is the
        admin library view (#217), so it spans every owner in the tenant (the caller
        is role-gated to ``admin`` one layer up). Ordered by ``(created_at, id)``
        **descending** with ``id`` the stable tiebreaker; the id-only cursor resolves
        the boundary ``created_at`` by a correlated scalar subquery (exact on Postgres
        + the offline SQLite), mirroring ``list_for_owner_page``.
        """
        conditions = [models.Assistant.tenant_id == self._tenant_id]
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

    async def get_by_number(self, assistant_id: UUID, version: int) -> AssistantVersion | None:
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
                models.Run.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]),
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

    async def mark_queued(self, run_id: UUID) -> Run | None:
        """Reset a run to ``queued`` for a re-drive (transient retry / human resume/reroute).

        Tenant-scoped. Clears the prior terminal so a re-delivered run task claims it
        again (``execute_run`` only runs a ``queued`` run). Clears ``finished_at`` +
        ``error`` (this attempt is superseded); ``started_at`` is stamped fresh on the
        next ``mark_running``. Used by the transient-retry fold and by the escalation
        resume/reroute handoff (#239) so an escalated run is re-driven, not stuck.
        """
        row = await self._get_row(run_id)
        if row is None:
            return None
        row.status = RunStatus.QUEUED.value
        row.started_at = None
        row.finished_at = None
        row.error = None
        await self._session.flush()
        return _to_run(row)

    async def reassign_owner(self, run_id: UUID, *, owner_id: UUID) -> Run | None:
        """Reassign a run to a different owner (the escalation *reroute* handoff, #239).

        Tenant-scoped (INV-1 — the new owner must be in the same tenant; the service
        enforces that). The run's ``owner_id`` is its execution **principal**: after a
        reroute the run retrieves only what the *new* owner could retrieve (INV-2), so
        a reroute never widens access. The caller pairs this with :meth:`mark_queued`
        to re-drive the run as the new owner.
        """
        row = await self._get_row(run_id)
        if row is None:
            return None
        row.owner_id = owner_id
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


class RunDeliveryRepository(_TenantScopedRepository):
    """In-app run-delivery records (``run_deliveries``) within one tenant (#238, ADR-0015 §6).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``delivery_id``
    resolves to ``None`` (the existence-non-disclosure 404 is enforced one layer up in
    ``services.run_delivery_service`` off the ``None`` return). Recipient visibility
    (deny-by-default, spec 0004 §2.2) is layered in the service. Persists via the
    session but does not commit — the caller owns the transaction boundary.

    Exposes ``create`` (produce one delivery on run completion), the recipient inbox
    read (``list_for_recipient_page``), ``mark_read`` (the owner opened it), and the
    pending-digest sweep (``list_pending_for_recipients`` / ``mark_delivered``) the
    digest beat drives.
    """

    async def create(
        self,
        *,
        recipient_id: UUID,
        run_id: UUID,
        schedule_id: UUID | None,
        kind: RunDeliveryKind,
        status: RunDeliveryStatus,
        summary: str | None,
    ) -> RunDelivery:
        """Create one in-app delivery of a completed run (ADR-0015 §6 — run inbox)."""
        row = models.RunDelivery(
            id=uuid4(),
            tenant_id=self._tenant_id,
            recipient_id=recipient_id,
            run_id=run_id,
            schedule_id=schedule_id,
            kind=kind.value,
            status=status.value,
            summary=summary,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_run_delivery(row)

    async def get(self, delivery_id: UUID) -> RunDelivery | None:
        row = await self._get_row(delivery_id)
        return _to_run_delivery(row) if row is not None else None

    async def _get_row(self, delivery_id: UUID) -> models.RunDelivery | None:
        stmt = select(models.RunDelivery).where(
            models.RunDelivery.tenant_id == self._tenant_id,
            models.RunDelivery.id == delivery_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def exists_for_run(self, run_id: UUID, *, kind: RunDeliveryKind) -> bool:
        """Whether a delivery of ``kind`` already exists for ``run_id`` (idempotency guard).

        The run task may redeliver (an at-least-once Celery message), so producing an
        inbox delivery is guarded on this so a re-run never double-notifies. Tenant-scoped.
        """
        stmt = (
            select(func.count())
            .select_from(models.RunDelivery)
            .where(
                models.RunDelivery.tenant_id == self._tenant_id,
                models.RunDelivery.run_id == run_id,
                models.RunDelivery.kind == kind.value,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0

    async def mark_read(self, delivery_id: UUID, *, read_at: datetime) -> RunDelivery | None:
        """Mark a delivery ``read`` and stamp ``read_at`` (idempotent), tenant-scoped.

        Re-marking an already-read delivery leaves ``read_at`` unchanged (the first
        open stands). Returns the updated entity, or ``None`` if no row matches.
        """
        row = await self._get_row(delivery_id)
        if row is None:
            return None
        if row.status != RunDeliveryStatus.READ.value:
            row.status = RunDeliveryStatus.READ.value
            row.read_at = read_at
            await self._session.flush()
        return _to_run_delivery(row)

    async def mark_delivered(self, delivery_id: UUID) -> RunDelivery | None:
        """Transition a ``pending`` digest delivery to ``delivered`` (the digest fired)."""
        row = await self._get_row(delivery_id)
        if row is None:
            return None
        row.status = RunDeliveryStatus.DELIVERED.value
        await self._session.flush()
        return _to_run_delivery(row)

    async def list_for_recipient_page(
        self,
        recipient_id: UUID,
        *,
        limit: int,
        after_id: UUID | None = None,
        status: RunDeliveryStatus | None = None,
        unread_only: bool = False,
    ) -> list[RunDelivery]:
        """A keyset page of a recipient's deliveries (newest first) — the run inbox.

        Recipient- *and* tenant-scoped (spec 0004 §2.2 + INV-1): a caller only ever
        sees their own deliveries. Optional ``status`` filter, and ``unread_only``
        excludes ``read`` deliveries (the unread inbox badge). Ordered by
        ``(created_at, id)`` descending; the id-only cursor resolves the boundary
        ``created_at`` by a correlated scalar subquery (exact on Postgres + the
        offline SQLite), mirroring the runs keyset.
        """
        conditions = [
            models.RunDelivery.tenant_id == self._tenant_id,
            models.RunDelivery.recipient_id == recipient_id,
        ]
        if status is not None:
            conditions.append(models.RunDelivery.status == status.value)
        if unread_only:
            conditions.append(models.RunDelivery.status != RunDeliveryStatus.READ.value)
        if after_id is not None:
            boundary_created_at = (
                select(models.RunDelivery.created_at)
                .where(
                    models.RunDelivery.tenant_id == self._tenant_id,
                    models.RunDelivery.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.RunDelivery.created_at < boundary_created_at,
                    and_(
                        models.RunDelivery.created_at == boundary_created_at,
                        models.RunDelivery.id < after_id,
                    ),
                )
            )
        stmt = (
            select(models.RunDelivery)
            .where(*conditions)
            .order_by(models.RunDelivery.created_at.desc(), models.RunDelivery.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_run_delivery(r) for r in rows]

    async def list_pending_digest(self, *, limit: int) -> list[RunDelivery]:
        """Every ``pending`` digest delivery in this tenant (oldest first) — the digest batch.

        The digest beat sweeps these per tenant to roll a day's low-urgency runs into
        one in-app notification (ADR-0015 §6). Tenant-scoped (INV-1); the caller runs
        it under ``tenant_session_scope`` per tenant. Bounded so a sweep never scans
        unboundedly (the beat re-runs to drain a backlog).
        """
        stmt = (
            select(models.RunDelivery)
            .where(
                models.RunDelivery.tenant_id == self._tenant_id,
                models.RunDelivery.status == RunDeliveryStatus.PENDING.value,
                models.RunDelivery.kind == RunDeliveryKind.DIGEST.value,
            )
            .order_by(models.RunDelivery.created_at.asc(), models.RunDelivery.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_run_delivery(r) for r in rows]


class RunDeliveryReconcileRepository:
    """Cross-tenant read of tenants with pending digest deliveries — the digest beat ONLY.

    **Not** tenant-scoped (it is the one delivery read that spans tenants), mirroring
    :class:`ScheduleReconcileRepository`: the periodic digest beat must find which
    tenants have ``pending`` digest deliveries so it can sweep each *as* that tenant
    (``tenant_session_scope``). It runs under a **bypass**-scoped session
    (``bind_bypass``) — a deliberate, system-only path, never a request path
    (requests are always tenant-scoped, INV-1). Read-only.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tenants_with_pending_digest(self) -> list[UUID]:
        """Distinct tenant ids that have at least one ``pending`` digest delivery."""
        stmt = (
            select(models.RunDelivery.tenant_id)
            .where(
                models.RunDelivery.status == RunDeliveryStatus.PENDING.value,
                models.RunDelivery.kind == RunDeliveryKind.DIGEST.value,
            )
            .distinct()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)


class SandboxSessionRepository(_TenantScopedRepository):
    """Tenant-scoped durable identities for reusable chat sandboxes (ADR-0020)."""

    async def get(self, sandbox_session_id: UUID) -> SandboxSession | None:
        stmt = select(models.SandboxSession).where(
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.id == sandbox_session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_sandbox_session(row) if row is not None else None

    async def get_for_chat(self, chat_session_id: UUID) -> SandboxSession | None:
        stmt = select(models.SandboxSession).where(
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.chat_session_id == chat_session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_sandbox_session(row) if row is not None else None

    async def get_or_create(
        self,
        *,
        owner_id: UUID,
        chat_session_id: UUID,
        image_digest: str,
    ) -> SandboxSession:
        """Return one active identity per tenant/chat, safe under concurrent first use."""
        existing = await self.get_for_chat(chat_session_id)
        if existing is not None:
            if existing.owner_id != owner_id:
                # A chat cannot legitimately change owners. Treat mismatched input as
                # nonexistent rather than ever reassigning a sandbox across principals.
                raise ValueError("sandbox session owner does not match the chat owner")
            if existing.status in (
                SandboxSessionStatus.CLOSED,
                SandboxSessionStatus.ERROR,
            ):
                advanced = await self.advance_generation(
                    existing.id,
                    image_digest=image_digest,
                    expected_generation=existing.generation,
                )
                if advanced is not None:
                    return advanced
                # Another request already reactivated/reset this identity. Reuse its
                # winner rather than incrementing a second time from stale state.
                winner = await self.get_for_chat(chat_session_id)
                if winner is None:  # pragma: no cover - row was read in this transaction
                    raise RuntimeError("sandbox session vanished during reactivation")
                return winner
            return existing

        row = models.SandboxSession(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            generation=1,
            status=SandboxSessionStatus.ACTIVE.value,
            image_digest=image_digest,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # Another worker won the unique (tenant, chat) insert. The savepoint
            # contains the violation, leaving the caller's transaction usable.
            winner = await self.get_for_chat(chat_session_id)
            if winner is None:  # pragma: no cover - defensive against a broken DB
                raise
            return winner
        return _to_sandbox_session(row)

    async def advance_generation(
        self,
        sandbox_session_id: UUID,
        *,
        image_digest: str | None = None,
        expected_generation: int | None = None,
    ) -> SandboxSession | None:
        """Atomically replace one generation; an expected-generation mismatch loses."""
        values: dict[str, object] = {
            "generation": models.SandboxSession.generation + 1,
            "status": SandboxSessionStatus.ACTIVE.value,
            "closed_at": None,
            "last_used_at": datetime.now(UTC),
        }
        if image_digest is not None:
            values["image_digest"] = image_digest
        predicate = [
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.id == sandbox_session_id,
        ]
        if expected_generation is not None:
            predicate.append(models.SandboxSession.generation == expected_generation)
        stmt = (
            update(models.SandboxSession)
            .where(*predicate)
            .values(**values)
            .returning(models.SandboxSession)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_sandbox_session(row) if row is not None else None

    async def touch(self, sandbox_session_id: UUID) -> SandboxSession | None:
        row = await self._get_row(sandbox_session_id)
        if row is None:
            return None
        row.last_used_at = datetime.now(UTC)
        await self._session.flush()
        return _to_sandbox_session(row)

    async def close(
        self, sandbox_session_id: UUID, *, expected_generation: int | None = None
    ) -> SandboxSession | None:
        now = datetime.now(UTC)
        predicate = [
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.id == sandbox_session_id,
        ]
        if expected_generation is not None:
            predicate.append(models.SandboxSession.generation == expected_generation)
        stmt = (
            update(models.SandboxSession)
            .where(*predicate)
            .values(
                status=SandboxSessionStatus.CLOSED.value,
                closed_at=now,
                last_used_at=now,
            )
            .returning(models.SandboxSession)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_sandbox_session(row) if row is not None else None

    async def mark_error(
        self, sandbox_session_id: UUID, *, expected_generation: int | None = None
    ) -> SandboxSession | None:
        predicate = [
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.id == sandbox_session_id,
        ]
        if expected_generation is not None:
            predicate.append(models.SandboxSession.generation == expected_generation)
        stmt = (
            update(models.SandboxSession)
            .where(*predicate)
            .values(
                status=SandboxSessionStatus.ERROR.value,
                last_used_at=datetime.now(UTC),
            )
            .returning(models.SandboxSession)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_sandbox_session(row) if row is not None else None

    async def _get_row(self, sandbox_session_id: UUID) -> models.SandboxSession | None:
        stmt = select(models.SandboxSession).where(
            models.SandboxSession.tenant_id == self._tenant_id,
            models.SandboxSession.id == sandbox_session_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class SourceReconcileRepository:
    """Cross-tenant read of connected managed sources — the sync-poll beat ONLY.

    **Not** tenant-scoped (the one source read that spans tenants), mirroring
    :class:`ScheduleReconcileRepository` / :class:`RunDeliveryReconcileRepository`:
    the periodic connector poll (ADR-0019 §3 cadence) must find every tenant's
    connected managed sources so it can enqueue each sync through the existing
    per-tenant rate-limited seam. Runs under a **bypass**-scoped session
    (``bind_bypass``) — a deliberate, system-only path, never a request path
    (requests stay tenant-scoped, INV-1). Read-only.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_connected_pollable(self) -> list[tuple[UUID, UUID]]:
        """``(tenant_id, source_id)`` for every pollable connected managed source.

        Pollable = carries a vault credential (``auth_secret_ref`` set — the
        managed marker) and sits in a resting status (``ready``/``error``) — a
        mid-``syncing``/``pending`` source already has a sync in flight, and a
        ``pending_auth`` source has no credential yet. Ordered for determinism.
        """
        stmt = (
            select(models.Source.tenant_id, models.Source.id)
            .where(
                models.Source.auth_secret_ref.is_not(None),
                models.Source.status.in_((SourceStatus.READY.value, SourceStatus.ERROR.value)),
            )
            .order_by(models.Source.created_at.asc(), models.Source.id.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def list_stranded_documents(
        self, *, older_than: datetime, limit: int
    ) -> list[tuple[UUID, UUID, DocumentKind]]:
        """``(tenant_id, document_id, kind)`` for documents stuck pre-ingestion.

        Covers both post-commit crash windows: an incremental connector page
        commits its row/cursor before task publication, and a direct upload
        commits its completed session/document before publication. In either
        case a broker fault can leave a durable ``pending``/``processing`` row
        with no live task. The idempotent pipeline is the recovery mechanism.

        The age threshold keeps a legitimately in-flight ingestion out of the
        result. Ready/failed rows and physically deleted rows are absent by the
        status/query predicates. Cross-tenant (bypass-scoped, system-only) and
        bounded by ``limit`` so one sweep can never fan out unbounded.
        """
        stmt = (
            select(models.Document.tenant_id, models.Document.id, models.Document.kind)
            .where(
                models.Document.status.in_(
                    (DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value)
                ),
                models.Document.updated_at < older_than,
            )
            .order_by(models.Document.updated_at.asc(), models.Document.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], DocumentKind(row[2])) for row in rows]


class CodeRunRepository(_TenantScopedRepository):
    """Sandbox code-run records within one tenant (ADR-0020 §4, #457).

    Tenant-scoped like every repository (INV-1): a foreign-tenant ``code_run_id``
    resolves to ``None`` (the existence-non-disclosure 404 is enforced one layer up
    in ``services.sandbox_service`` off the ``None`` return). Owner visibility is
    layered in the service (deny-by-default, spec 0004 §2.2). Persists via the
    session but does not commit — the caller owns the transaction boundary.

    The run's ``status`` is written as the sandbox walks the state machine
    (``queued`` → ``running`` → a terminal); a crash-safe task always writes a
    terminal, never leaving a stuck ``running`` (ADR-0020 §4, INV-8). Legacy
    ADR-0013 accounting readers remain for wire/data compatibility but ADR-0020
    does not enforce those caps.
    """

    async def create(
        self,
        *,
        owner_id: UUID,
        code: str,
        status: CodeRunStatus = CodeRunStatus.QUEUED,
        session_id: UUID | None = None,
        run_id: UUID | None = None,
        trace_id: UUID | None = None,
        code_run_id: UUID | None = None,
        requested_packages: tuple[str, ...] = (),
    ) -> CodeRun:
        """Create a ``queued`` (or already-``denied``) code run (ADR-0020 §4).

        ``code_run_id`` may be pre-minted so it doubles as the WS ``runId`` known to
        the enqueuer before the task starts (the ``code_output``/``code_result``
        correlation id); omitted ⇒ generated. A run refused before execution (quota /
        disabled tenant, §6) is created directly with ``status=denied``.
        """
        row = models.CodeRun(
            id=code_run_id or uuid4(),
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
            status=status.value,
            code=code,
            requested_packages=list(requested_packages),
            stdout="",
            stderr="",
            artifact_ids=[],
        )
        self._session.add(row)
        await self._session.flush()
        return _to_code_run(row)

    async def get(self, code_run_id: UUID) -> CodeRun | None:
        stmt = select(models.CodeRun).where(
            models.CodeRun.tenant_id == self._tenant_id,
            models.CodeRun.id == code_run_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_code_run(row) if row is not None else None

    async def count_active(self) -> int:
        """Count active runs for legacy ADR-0013 accounting; not an ADR-0020 gate."""
        stmt = (
            select(func.count())
            .select_from(models.CodeRun)
            .where(
                models.CodeRun.tenant_id == self._tenant_id,
                models.CodeRun.status.in_(
                    [CodeRunStatus.QUEUED.value, CodeRunStatus.RUNNING.value]
                ),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_executing(self) -> int:
        """Runs EXECUTING right now — the `max_concurrency` admission gate (#519).

        Deliberately not :meth:`count_active`, which also counts ``QUEUED``. A queued
        run holds no runner thread and no runner memory, so counting it would refuse
        work on the basis of a queue depth that costs nothing — and, because a run is
        already ``QUEUED`` when its own admission check runs, it would count ITSELF and
        make the gate off by one.

        `max_concurrency` therefore means what an operator would assume: how many of
        this tenant's runs may occupy the sandbox at the same moment.
        """
        stmt = (
            select(func.count())
            .select_from(models.CodeRun)
            .where(
                models.CodeRun.tenant_id == self._tenant_id,
                models.CodeRun.status == CodeRunStatus.RUNNING.value,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def runtime_ms_since(self, since: datetime) -> int:
        """Sum duration for legacy ADR-0013 accounting; not an ADR-0020 gate."""
        stmt = select(func.coalesce(func.sum(models.CodeRun.duration_ms), 0)).where(
            models.CodeRun.tenant_id == self._tenant_id,
            models.CodeRun.finished_at.is_not(None),
            models.CodeRun.finished_at >= since,
        )
        total = (await self._session.execute(stmt)).scalar_one()
        return int(total or 0)

    async def mark_running(
        self,
        code_run_id: UUID,
        *,
        started_at: datetime,
        image_digest: str | None = None,
        sandbox_session_id: UUID | None = None,
        sandbox_generation: int | None = None,
    ) -> CodeRun | None:
        """Atomically transition ``queued`` to ``running``; otherwise return current."""
        values: dict[str, object] = {
            "status": CodeRunStatus.RUNNING.value,
            "started_at": started_at,
        }
        if image_digest is not None:
            values["image_digest"] = image_digest
        if sandbox_session_id is not None:
            values["sandbox_session_id"] = sandbox_session_id
        if sandbox_generation is not None:
            values["sandbox_generation"] = sandbox_generation
        stmt = (
            update(models.CodeRun)
            .where(
                models.CodeRun.tenant_id == self._tenant_id,
                models.CodeRun.id == code_run_id,
                models.CodeRun.status == CodeRunStatus.QUEUED.value,
            )
            .values(**values)
            .returning(models.CodeRun)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return _to_code_run(row)
        current = await self._get_row(code_run_id, populate_existing=True)
        return _to_code_run(current) if current is not None else None

    async def mark_terminal(
        self,
        code_run_id: UUID,
        *,
        status: CodeRunStatus,
        finished_at: datetime,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        duration_ms: int | None = None,
        resource_usage: ResourceUsage | None = None,
        image_digest: str | None = None,
        artifact_ids: list[UUID] | None = None,
        resolved_packages: tuple[dict[str, str], ...] | None = None,
    ) -> CodeRun | None:
        """Write a run's terminal status + captured result (ADR-0013 §4/§5), tenant-scoped.

        The single terminal-writing method: sets one of ``succeeded``/``failed``/
        ``timeout``/``killed``/``denied``, ``finished_at``, and the captured
        output/exit/timing/resource-usage/artifacts. A crash path calls this with
        ``failed`` + an error message on ``stderr`` so a run never ends in silence
        (INV-8, never a stuck ``running``). Terminal states are immutable: when
        cancellation and normal completion race, the first terminal transition wins.
        """
        if status in (CodeRunStatus.QUEUED, CodeRunStatus.RUNNING):
            raise ValueError("mark_terminal requires a terminal code-run status")
        values: dict[str, object] = {
            "status": status.value,
            "finished_at": finished_at,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "resource_usage": resource_usage.to_dict() if resource_usage is not None else None,
            "artifact_ids": [str(a) for a in (artifact_ids or [])],
        }
        if image_digest is not None:
            values["image_digest"] = image_digest
        if resolved_packages is not None:
            # Only written when the runner reported one. Writing `[]` unconditionally
            # would turn "we did not learn what this run installed" into "this run
            # installed nothing" — a false statement in the record that exists to
            # answer that exact question (#509).
            values["resolved_packages"] = [dict(entry) for entry in resolved_packages]
        stmt = (
            update(models.CodeRun)
            .where(
                models.CodeRun.tenant_id == self._tenant_id,
                models.CodeRun.id == code_run_id,
                models.CodeRun.status.in_(
                    [CodeRunStatus.QUEUED.value, CodeRunStatus.RUNNING.value]
                ),
            )
            .values(**values)
            .returning(models.CodeRun)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return _to_code_run(row)
        current = await self._get_row(code_run_id, populate_existing=True)
        return _to_code_run(current) if current is not None else None

    async def _get_row(
        self, code_run_id: UUID, *, populate_existing: bool = False
    ) -> models.CodeRun | None:
        stmt = select(models.CodeRun).where(
            models.CodeRun.tenant_id == self._tenant_id,
            models.CodeRun.id == code_run_id,
        )
        if populate_existing:
            stmt = stmt.execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()


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


def _to_session_summary(row: models.SessionSummary) -> SessionSummary:
    pairs: list[tuple[UUID, UUID]] = []
    for item in (row.evidence or [])[:_EVIDENCE_MAX_PAIRS]:
        # Defensive read (the column is data): only well-formed id pairs count,
        # and the cardinality cap holds on READ too — a corrupt/hostile row
        # must not fan out into hundreds of permission checks (#446 finding 4).
        try:
            pairs.append((UUID(str(item["document_id"])), UUID(str(item["chunk_id"]))))
        except (KeyError, TypeError, ValueError):
            continue
    mentioned: list[tuple[UUID, str]] = []
    raw_mentioned = row.mentioned_documents if isinstance(row.mentioned_documents, dict) else {}
    for key, name in list(raw_mentioned.items())[:_MENTIONED_MAX_ENTRIES]:
        try:
            mentioned.append((UUID(str(key)), str(name)))
        except (TypeError, ValueError):
            continue
    return SessionSummary(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        summary=(row.summary[:_SUMMARY_MAX_CHARS] if row.summary else row.summary),
        covers_through_message_id=row.covers_through_message_id,
        covers_through_created_at=row.covers_through_created_at,
        evidence=tuple(pairs),
        mentioned_documents=tuple(mentioned),
        version=row.version,
        updated_at=row.updated_at,
    )


# Bounds on the stored memory state (#446 finding 4): enforced at WRITE and
# defensively re-applied at READ, so neither a growing session nor a corrupt
# row can fan out into unbounded permission checks or a context refusal.
_EVIDENCE_MAX_PAIRS = 20
_SUMMARY_MAX_CHARS = 6_000
# The mention map's own cap (#446 round-3): LARGER than the evidence cap —
# it merges forward across passes, and every name the rolled-forward text may
# still carry must stay redactable (blocker 1's >20-name gap).
_MENTIONED_MAX_ENTRIES = 40


class SessionSummaryRepository(_TenantScopedRepository):
    """The per-session rolling summary + evidence digest (#416, ADR-0016 §3.2).

    Tenant-scoped (INV-1). One row per session, maintained by TWO writers with
    different transactions: the answer path (evidence, its own transaction) and
    the async summarizer (summary text, a Celery transaction). Every write is
    an ATOMIC upsert (#446 finding 3): a dialect-native ``INSERT .. ON
    CONFLICT`` claims the row, and the summary's forward-only rule is a
    compare-and-swap on the coverage cursor — a unique-key race can neither
    surface into the answer path nor let a stale task regress coverage.
    Flushed, not committed — each caller owns its transaction.
    """

    def _insert(self) -> object:
        bind = self._session.bind
        if bind is not None and bind.dialect.name == "sqlite":
            return sqlite_upsert(models.SessionSummary)
        return pg_upsert(models.SessionSummary)

    async def get_for_session(self, session_id: UUID) -> SessionSummary | None:
        stmt = select(models.SessionSummary).where(
            models.SessionSummary.tenant_id == self._tenant_id,
            models.SessionSummary.session_id == session_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_session_summary(row) if row is not None else None

    async def upsert_evidence(
        self, session_id: UUID, *, evidence: list[tuple[UUID, UUID]]
    ) -> SessionSummary | None:
        """Replace the session's evidence digest with the LAST answer's ids.

        IDs only (ADR-0016 §3.2), capped at ``_EVIDENCE_MAX_PAIRS`` (first-cited
        wins), empty clears. ATOMIC: a dialect-native upsert — a concurrent
        summarizer's row claim degrades this to an UPDATE of the evidence
        column only, never a unique-key error inside the answer transaction
        (#446 finding 3). Touches nothing the summarizer owns.
        """
        payload = [
            {"document_id": str(d), "chunk_id": str(c)} for d, c in evidence[:_EVIDENCE_MAX_PAIRS]
        ]
        insert = self._insert().values(  # type: ignore[attr-defined]
            id=uuid_mod.uuid4(),
            tenant_id=self._tenant_id,
            session_id=session_id,
            evidence=payload,
        )
        stmt = insert.on_conflict_do_update(
            index_elements=["tenant_id", "session_id"],
            set_={"evidence": payload},
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return await self.get_for_session(session_id)

    async def upsert_summary(
        self,
        session_id: UUID,
        *,
        summary: str,
        covers_through_message_id: UUID,
        covered_created_at: datetime,
        mentioned_documents: dict[UUID, str] | None = None,
    ) -> tuple[bool, SessionSummary | None]:
        """Advance the rolling summary; returns ``(accepted, row)``.

        Forward-only via compare-and-swap on the coverage cursor (#446 findings
        3/5): the UPDATE lands only where the stored cursor is NULL or older
        under the ``(created_at, id)`` total order — a stale/racing task's
        write is a no-op with ``accepted=False``, so its caller neither bumps
        ``version`` nor emits the summarized audit. Same-timestamp advances to
        a DIFFERENT boundary id are allowed (second-resolution timestamps);
        the id inequality keeps an identical rewrite idempotent. The summary
        text and the mention map are size-capped here (the write chokepoint).
        """
        text_value = summary[:_SUMMARY_MAX_CHARS]
        mentioned_payload = {
            str(k): str(v)[:200]
            for k, v in list((mentioned_documents or {}).items())[:_MENTIONED_MAX_ENTRIES]
        }
        insert = self._insert().values(  # type: ignore[attr-defined]
            id=uuid_mod.uuid4(),
            tenant_id=self._tenant_id,
            session_id=session_id,
            summary=text_value,
            covers_through_message_id=covers_through_message_id,
            covers_through_created_at=covered_created_at,
            mentioned_documents=mentioned_payload,
            version=1,
        )
        stmt = insert.on_conflict_do_update(
            index_elements=["tenant_id", "session_id"],
            set_={
                "summary": text_value,
                "covers_through_message_id": covers_through_message_id,
                "covers_through_created_at": covered_created_at,
                "mentioned_documents": mentioned_payload,
                "version": models.SessionSummary.version + 1,
            },
            where=(
                models.SessionSummary.covers_through_created_at.is_(None)
                | (models.SessionSummary.covers_through_created_at < covered_created_at)
                | (
                    (models.SessionSummary.covers_through_created_at == covered_created_at)
                    # ORDERED tie-break (#446 round-2 blocker 3): within one
                    # second, coverage may only advance toward the LARGER
                    # boundary id — an arbitrary but STABLE total order, so a
                    # stale A can never be re-accepted after B (the A->B->A
                    # probe). A same-second boundary with a smaller id waits
                    # for the next pass (a strictly-newer timestamp) — progress
                    # converges, regression cannot happen.
                    & (models.SessionSummary.covers_through_message_id < covers_through_message_id)
                )
            ),
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        row = await self.get_for_session(session_id)
        accepted = bool(getattr(result, "rowcount", 0)) and (
            row is not None and row.covers_through_message_id == covers_through_message_id
        )
        return accepted, row
