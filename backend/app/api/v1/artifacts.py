"""Artifacts routes — list / get / content / delete agent-produced files (#222).

Contract-first: shapes match ``contracts/openapi.yaml`` (``Artifact`` /
``ArtifactList`` with cursor pagination; ``ArtifactProducedBy`` + ``session_id``
filters; ``302`` + ``Location`` presigned content, like ``documents``). This
module exposes a module-level ``router`` and is **auto-discovered**
(``api/v1/__init__.py`` scans for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, object-store access, audit, the
cursor codec) lives in ``services.artifacts_service`` (already merged from #208);
this layer only (de)serialises and threads correlation context. Object storage is
reached **only** through the #22 ``storage/`` adapter (held by the service), never
here; SQL only via ``db/``.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** An
artifact's ``tenant_id`` and ``owner_id`` come from the resolved principal
(``current_user`` / ``current_tenant`` — the token, never request input). An
artifact in another tenant or owned by another user is reported as **404**
(existence non-disclosure): the service returns ``None``/``False`` and this router
maps that to ``NotFoundError``, never 403. Every route requires the bearer token
(contract ``bearerAuth``); unauthenticated → 401 via ``current_user`` (INV-4). A
malformed (non-uuid) id is **422** (FastAPI path validation).

This is a **read + delete** surface: artifacts are *produced* by the file-writing
tool (#220) and the code sandbox (#230/#231) off the request path — there is
deliberately no create endpoint here (agents write through the service seam).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    ObjectStoreDep,
    SettingsDep,
    extract_request_id,
)
from app.core.errors import NotFoundError
from app.domain.entities import Artifact, ArtifactProducedBy
from app.services.artifacts_service import ArtifactPage, ArtifactsService

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class ArtifactResponse(BaseModel):
    """``#/components/schemas/Artifact`` — the wire projection of an artifact."""

    model_config = {"extra": "forbid"}

    id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    owner_id: UUID
    produced_by: ArtifactProducedBy
    created_at: datetime
    session_id: UUID | None = None
    run_id: UUID | None = None
    tool_invocation_id: UUID | None = None


class ArtifactListResponse(BaseModel):
    """``#/components/schemas/ArtifactList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[ArtifactResponse]
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_response(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=artifact.id,
        filename=artifact.filename,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        owner_id=artifact.owner_id,
        produced_by=artifact.produced_by,
        created_at=artifact.created_at,
        session_id=artifact.session_id,
        run_id=artifact.run_id,
        tool_invocation_id=artifact.tool_invocation_id,
    )


def _to_list_response(page: ArtifactPage) -> ArtifactListResponse:
    return ArtifactListResponse(
        items=[_to_response(a) for a in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
    request: Request,
) -> ArtifactsService:
    """Assemble the per-request service from the identity + adapter + audit seams.

    Mirrors ``documents._build_service``: the object store is the injected #22
    adapter (the only object-store caller), the audit sink is bound to the
    caller's tenant, and ``request_id``/``source_ip`` thread the correlation
    context into audit envelopes without the service touching the request. The
    allowlist/cap + retention window are only used by the (out-of-scope-here)
    ``create`` path; they are still supplied so the service is fully constructed.
    """
    return ArtifactsService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        object_store=object_store,
        audit=make_audit_sink(tenant_id),
        # The audit envelope requires a non-empty request_id / source_ip (spec
        # 0004 §2.4); fall back to a sentinel when the client supplied neither.
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
        artifact_allowed_content_types=settings.artifact_allowed_content_types,
        max_artifact_bytes=settings.max_artifact_bytes,
        retention_days=settings.artifact_retention_days,
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=ArtifactListResponse, response_model_exclude_none=True)
async def list_artifacts(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
    session_id: Annotated[UUID | None, Query()] = None,
    produced_by: Annotated[ArtifactProducedBy | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ArtifactListResponse:
    """List the caller's own produced artifacts (cursor-paginated, newest first).

    Optional filters mirror the contract: ``session_id`` restricts to one chat
    session, ``produced_by`` to one write origin (chat_session / run / tool).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    page = await service.list_artifacts(
        cursor=cursor,
        limit=limit,
        produced_by=produced_by,
        session_id=session_id,
    )
    return _to_list_response(page)


@router.get("/{artifact_id}", response_model=ArtifactResponse, response_model_exclude_none=True)
async def get_artifact(
    artifact_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> ArtifactResponse:
    """Get one of the caller's artifacts' metadata; not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    artifact = await service.get_artifact(artifact_id)
    if artifact is None:
        raise NotFoundError("Artifact not found.")
    return _to_response(artifact)


@router.get("/{artifact_id}/content")
async def get_artifact_content(
    artifact_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> Response:
    """Download / view the artifact bytes; not visible → 404.

    Default (``DOCUMENT_CONTENT_REDIRECT=true``): respond **302** to a short-TTL
    presigned GET URL (CC-12) via the #22 ``ObjectStore.presign_get_artifact``,
    with the ``Location`` header — the client follows it and the bytes transfer
    directly from storage, not through the API process. When the flag is false,
    stream the bytes inline as the artifact's own content-type. Audited
    ``artifact.downloaded`` (INV-6) by the service. Mirrors
    ``documents.get_document_content``.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )

    if settings.document_content_redirect:
        url = await service.presign_artifact_content(artifact_id)
        if url is None:
            raise NotFoundError("Artifact not found.")
        await session.commit()
        # 302 + Location per the frozen contract; the client must follow it.
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

    content = await service.get_artifact_content(artifact_id)
    if content is None:
        raise NotFoundError("Artifact not found.")
    await session.commit()
    # Stream with the artifact's own (allowlisted) content-type so a browser can
    # render a preview inline. The artifact allowlist admits image/svg+xml and
    # text/html, which CAN carry active content — so this same-origin inline path
    # is served with an attachment disposition (forces download, never inline
    # render) and no-sniff, so the browser never executes an artifact as a page.
    # The redirect path (default) sidesteps this entirely by handing off to a
    # presigned storage URL. The UI's own preview sanitises separately (#222).
    return StreamingResponse(
        iter((content.data,)),
        media_type=content.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{content.filename}"',
            "Content-Length": str(len(content.data)),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    artifact_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> Response:
    """Delete one of the caller's artifacts (stored object + row); else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    deleted = await service.delete_artifact(artifact_id)
    if not deleted:
        raise NotFoundError("Artifact not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
