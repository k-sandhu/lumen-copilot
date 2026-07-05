"""Per-user account use-cases — the caller managing their OWN account.

The orchestration behind the ``/me`` routes (ADR-0004: ``services/`` compose the
``db/`` repositories + the object store; the router calls exactly one service).
Distinct from :mod:`app.services.admin_service` (tenant-wide governance an *admin*
performs): here a user manages their **own** account — currently their profile
avatar — so the surface is authenticated but **not** admin-gated (INV-5 plays no
role; the write only ever touches the caller's own row).

Tenant binding (spec 0004 §2.3): the tenant id is the one resolved from the
caller's token (``current_tenant``), passed in by the router — never request
input. The ``user_id`` is the resolved principal's own id; there is no path
parameter, so a user can only ever act on themselves (INV-1/INV-2).

* :meth:`UserService.set_user_avatar` / :meth:`UserService.clear_user_avatar`
  — set or clear the per-user profile avatar: store the image via the object store
  (keyed ``{tenant_id}/{user_id}/{sha}/{name}`` so it is scoped to the owning user),
  persist its key on the user row (``User.avatar_key``; ``None`` ⇒ the initials
  fallback), and return a presigned GET URL. Reversible actions, audited
  (``user.avatar_updated``, INV-6). An over-size avatar maps to **413** and a
  non-image to **415** (mirroring the logo's validation-error remap).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.repositories import AuditEventRepository, UserRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.services.audit import AuditSink
from app.storage import ObjectStore


def _map_avatar_validation_error(exc: ValidationError) -> Exception:
    """Re-map a storage avatar-validation error to the contract's HTTP status.

    ``ObjectStore.put_avatar`` reuses the single ``validate_upload`` allowlist +
    size-cap rule owner, which raises a generic ``ValidationError`` (422) with a
    stable ``code``. The avatar contract pins distinct statuses for the upload
    negatives, so map by code here: ``upload_too_large`` → 413,
    ``content_type_not_allowed`` → 415; an empty/other rejection stays 422 (INV-8).
    Mirrors ``admin_service._map_logo_validation_error``.
    """
    if exc.code == "upload_too_large":
        return PayloadTooLargeError(exc.detail, code=exc.code)
    if exc.code == "content_type_not_allowed":
        return UnsupportedMediaTypeError(exc.detail, code=exc.code)
    return exc


class UserService:
    """Manage the caller's own account (spec: user settings page).

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``user_id`` (both from the token). The user only ever acts on themselves — there
    is no id in the path — so tenancy + ownership are structural (INV-1/INV-2).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._users = UserRepository(session, tenant_id)

    async def set_user_avatar(
        self,
        *,
        object_store: ObjectStore,
        avatar_bytes: bytes,
        content_type: str,
        filename: str,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> str:
        """Store the caller's profile avatar and return a presigned GET URL.

        A reversible action a user takes on their OWN account (not admin-gated),
        audited here (INV-6). The bytes are validated against the image-only avatar
        allowlist/limit (reused from the logo config) and stored via
        ``object_store.put_avatar`` (the only object-store caller); the resulting key
        is persisted on the user row so the shell renders their avatar. An over-size
        avatar maps to **413** and a non-image to **415**. Returns a short-TTL
        presigned GET URL the shell renders immediately.
        """
        try:
            stored = await object_store.put_avatar(
                tenant_id=str(self._tenant_id),
                user_id=str(self._user_id),
                data=avatar_bytes,
                content_type=content_type,
                filename=filename,
            )
        except ValidationError as exc:
            raise _map_avatar_validation_error(exc) from exc

        updated = await self._users.set_avatar_key(self._user_id, avatar_key=stored.key)
        if updated is None:
            # Unreachable in practice (the caller's own user, resolved from the token,
            # exists); guard so a vanished user is a clean 404, not a 500.
            raise NotFoundError("User not found.")

        await self._emit_avatar_audit(
            actor_id=actor_id,
            request_id=request_id,
            source_ip=source_ip,
            has_avatar=True,
        )
        return await object_store.presign_get(str(self._tenant_id), stored.key)

    async def clear_user_avatar(
        self,
        *,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> None:
        """Clear the caller's profile avatar so the initials fallback applies again.

        The reverse of :meth:`set_user_avatar` (audited): sets the user's
        ``avatar_key`` to ``None``. Idempotent — clearing an already-unset avatar is a
        no-op mutation that still records the audited action. The stored object is left
        in place (content-addressed, unreferenced); no bytes are read.
        """
        updated = await self._users.set_avatar_key(self._user_id, avatar_key=None)
        if updated is None:
            raise NotFoundError("User not found.")
        await self._emit_avatar_audit(
            actor_id=actor_id,
            request_id=request_id,
            source_ip=source_ip,
            has_avatar=False,
        )

    async def _emit_avatar_audit(
        self,
        *,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
        has_avatar: bool,
    ) -> None:
        """Emit the avatar-update audit event (INV-6 / mission filter #4).

        The action ran and is attributed to the user who made it (their own account);
        ``has_avatar`` on the metadata records whether an avatar was set or cleared
        (the object key itself is not audited — it is derivable and not a
        security-relevant value). ``resource_id`` is the user's own id.
        """
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.USER_AVATAR_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="user",
            resource_id=str(self._user_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={"has_avatar": has_avatar},
        )
