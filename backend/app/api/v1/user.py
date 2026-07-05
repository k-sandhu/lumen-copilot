"""Per-user account routes — the caller managing their OWN account (``/me/*``).

Contract-first: shapes match ``contracts/openapi.yaml`` (``UserAvatar``). The
router validates in → calls **one** service → shapes out (ADR-0004). Distinct from
``/admin/*`` (governance an *admin* performs): here a user manages their **own**
account, so every route is authenticated (INV-4) but **not** admin-gated — the
write only ever touches the caller's own row (there is no id in the path, so
INV-1/INV-2 are structural).

The one surface today is the profile avatar: ``PUT /me/avatar`` (multipart) stores
the image and persists its key on the user row (the shell reads the resulting
presigned URL from ``GET /auth/me``); ``DELETE /me/avatar`` clears it (initials
fallback). Both are audited (``user.avatar_updated``, INV-6). Over-size → **413**, a
non-image content-type → **415**, a missing/blank filename → **422** (INV-8).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.api.deps import (
    CurrentTenant,
    CurrentUser,
    DbSession,
    ObjectStoreDep,
    extract_request_id,
)
from app.core.errors import ValidationError
from app.services.user_service import UserService

router = APIRouter(prefix="/me", tags=["user"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class UserAvatarResponse(BaseModel):
    """``#/components/schemas/UserAvatar`` — the caller's profile-avatar URL.

    ``avatar_url`` is a short-TTL presigned GET URL for the caller's uploaded avatar,
    or ``null`` when none is set (the shell then renders the initials fallback).
    """

    model_config = {"extra": "forbid"}

    avatar_url: str | None = None


@router.put("/avatar", response_model=UserAvatarResponse)
async def update_avatar(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    object_store: ObjectStoreDep,
    file: Annotated[UploadFile, File()],
) -> UserAvatarResponse:
    """Upload the caller's profile avatar (authenticated; audited).

    A user sets the profile picture shown in the app shell for their own account.
    Stores the image (MinIO, #22) keyed to the caller within their tenant and
    persists its key on the user row; the shell reads the resulting presigned URL
    from ``GET /auth/me``. Authenticated via the bearer (INV-4); tenant-scoped via
    ``current_tenant`` (INV-1) and scoped to the caller's own row (INV-2); the
    service audits the change (INV-6). Over-size → **413**, a non-image content-type
    → **415**, a missing/blank filename → **422**.
    """
    data = await file.read()
    # The declared content-type drives the allowlist check; default to the generic
    # octet-stream so a client that omits it is rejected by the allowlist (415)
    # rather than silently accepted.
    content_type = file.content_type or "application/octet-stream"
    filename = (file.filename or "").strip()
    if not filename:
        raise ValidationError("upload is missing a filename", code="missing_filename")

    service = UserService(session, tenant_id=tenant_id, user_id=principal.user_id)
    avatar_url = await service.set_user_avatar(
        object_store=object_store,
        avatar_bytes=data,
        content_type=content_type,
        filename=filename,
        actor_id=principal.user_id,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    await session.commit()
    return UserAvatarResponse(avatar_url=avatar_url)


@router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def clear_avatar(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> Response:
    """Clear the caller's profile avatar so the initials fallback applies again.

    The reverse of ``PUT /me/avatar``: sets the caller's ``avatar_key`` to null.
    Authenticated via the bearer (INV-4); scoped to the caller's own row
    (INV-1/INV-2); the service audits the change (INV-6). Idempotent — clearing an
    already-unset avatar succeeds with **204**.
    """
    service = UserService(session, tenant_id=tenant_id, user_id=principal.user_id)
    await service.clear_user_avatar(
        actor_id=principal.user_id,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
