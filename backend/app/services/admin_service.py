"""Admin governance use-cases — read-mostly /admin/* (issue #87).

The orchestration layer behind the admin console surfaces (ADR-0004: ``services/``
compose adapters; routers call exactly one service). The governance surfaces
(members, model governance, risk tiers) are **reads** — the admin console
*reflects* governance, it never changes it (read-before-write, spec 0004 §2.5 /
mission filter #3). The one **write** is the per-tenant settings update (issue
#148): a reversible, tenant-scoped **T1** action (spec 0004 §2.5 — "authorized
owner; audited; no extra approval") that sets a tenant's chat tool-turn budget.
It is admin-gated one layer up (INV-5) and audited here (INV-6); it touches only
the caller's own tenant's operational config, never another tenant and no T2+
governance.

Use-cases:

* :meth:`AdminService.list_members` — the tenant's roster (id, email, roles),
  cursor-paginated. Tenant-scoped (INV-1): only the caller's own tenant's
  members are returned. Role gating (admin-only, INV-5) is enforced one layer up
  by the router's ``require_roles(Role.ADMIN)`` dependency, before this is called.
* :meth:`AdminService.model_governance` — which models are permitted and the
  governance tiers they map to, drawn from the curated model registry (#47,
  config in ``core/config.py``) via :class:`ChatModelService`. No new table.
* :meth:`AdminService.risk_tiers` — the read-before-write risk-tier reference
  (T0–T3, spec 0004 §2.5) as **static config**, not a table (the tiers are a
  fixed product policy, not per-tenant data).
* :meth:`AdminService.get_tenant_settings` / :meth:`AdminService.update_tenant_settings`
  — read and write the per-tenant admin settings (issue #148): the chat tool-turn
  budget override (``Tenant.max_tool_turns``); ``None`` ⇒ the system default.
* :meth:`AdminService.set_tenant_logo` / :meth:`AdminService.clear_tenant_logo`
  — set or clear the per-tenant application logo (admin branding): store the image
  via the object store, persist its key on the tenant row (``Tenant.logo_key``;
  ``None`` ⇒ the default brand mark), and return a presigned GET URL. Reversible,
  tenant-scoped **T1** actions, audited like the settings write.

Tenant binding (spec 0004 §2.3): the tenant id is the one resolved from the
caller's token (``current_tenant``), passed in by the router — never request
input. ``owner_id`` plays no role here: the members roster is tenant-wide (the
admin sees *every* member of their tenant), which is exactly why the gate is the
``admin`` role rather than ownership.

Cursor pagination mirrors the collections/documents keyset: an opaque cursor
encodes the **id** of the last item of the previous page over ``(created_at,
id)`` ascending; the service fetches one more than the page size to detect a
next page. A malformed cursor is rejected fail-closed (422, INV-8).

Boundary note (ADR-0004 / ADR-0008 §1): listing *every* user in a tenant is a
read the shared ``db/UserRepository`` does not yet expose, and that repository
lives in the wave-0 ``db/repositories.py`` seam this slice may not edit. So the
tenant-scoped, parameterized user query is localized here in
:class:`_TenantMemberQuery` as a deliberate, narrow exception — it still goes
through the injected ``AsyncSession`` and the ``db/`` ORM models, applies the
``tenant_id`` predicate on every query (INV-1, structural — there is no method
that omits it), and returns domain :class:`~app.domain.entities.User` values, not
ORM rows. When ``db/repositories.py`` is next opened (a wave-0 change), this
``list_for_tenant_page`` belongs on ``UserRepository``; tracked as a residual
risk in the PR, not silently absorbed.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db import models
from app.db.repositories import AuditEventRepository, TenantRepository, UserRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Role, User
from app.domain.models import ModelTier
from app.services.audit import AuditSink, PermissionDeniedContext
from app.services.models_service import ChatModelService
from app.storage import ObjectStore

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


# --- Read-before-write risk tiers (spec 0004 §2.5) — STATIC product policy ---
#
# The T0–T3 tiers are a fixed product policy, not per-tenant data, so they live
# as config here rather than in a table (issue #87: "prefer STATIC config"). The
# text traces directly to spec 0004 §2.5; the contract's ``RiskTier`` requires
# ``{tier, description, approval}`` with ``tier`` in the enum [T0, T1, T2, T3].


@dataclass(frozen=True, slots=True)
class RiskTierView:
    """One read-before-write risk tier (contract ``RiskTier`` schema)."""

    tier: str
    description: str
    approval: str


_RISK_TIERS: tuple[RiskTierView, ...] = (
    RiskTierView(
        tier="T0",
        description="Read-only: retrieve, answer, summarize, draft-in-chat. The entire MVP is T0.",
        approval="none",
    ),
    RiskTierView(
        tier="T1",
        description=(
            "Reversible internal write: create a collection, upload or delete "
            "your own document, rename."
        ),
        approval="authorized owner; audited; no extra approval",
    ),
    RiskTierView(
        tier="T2",
        description=(
            "Consequential / external write: write back to a source, send, "
            "external share. Out of MVP."
        ),
        approval="explicit human approval in-session + a stated risk tier",
    ),
    RiskTierView(
        tier="T3",
        description=(
            "Destructive / irreversible external: bulk delete, change source "
            "permissions. Out of MVP."
        ),
        approval="explicit human approval + confirmation",
    ),
)


# --- Model governance (drawn from the curated registry, #47) ----------------
#
# The governance "tier" dimension is the model registry's :class:`ModelTier`
# (frontier / fast / oss): which models are approved, grouped by capability tier.
# Descriptions trace to ``domain.models.ModelTier``.

_MODEL_TIER_DESCRIPTIONS: dict[ModelTier, str] = {
    ModelTier.FRONTIER: "Highest-quality flagship models.",
    ModelTier.FAST: "Lower-latency, lower-cost models.",
    ModelTier.OSS: "Open-weight models.",
}


@dataclass(frozen=True, slots=True)
class ModelGovernanceEntryView:
    """One allowed model + the governance tier it maps to (contract schema)."""

    model_id: str
    tier: str
    label: str | None


@dataclass(frozen=True, slots=True)
class GovernanceTierView:
    """A governance tier referenced by an allowed model (contract schema)."""

    id: str
    description: str


@dataclass(frozen=True, slots=True)
class ModelGovernanceView:
    """The read-only model-governance view (contract ``ModelGovernance``)."""

    allowed_models: list[ModelGovernanceEntryView]
    tiers: list[GovernanceTierView]


# --- Members roster ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberPage:
    """One page of members plus the opaque cursor for the next page."""

    items: list[User]
    next_cursor: str | None


# --- Per-tenant admin settings (issue #148) ---------------------------------


@dataclass(frozen=True, slots=True)
class TenantSettingsView:
    """The admin-configurable per-tenant settings (contract ``TenantSettings``).

    ``max_tool_turns`` is the **effective** chat tool-turn budget — the tenant's
    override if set, else the system default; ``max_tool_turns_is_default`` says
    which, so the admin console can show "default (20)" vs an explicit override.
    ``fallback_models`` is the ordered turn-failover list (ADR-0016 §4, #413) —
    empty ⇒ no fallback configured.
    """

    max_tool_turns: int
    max_tool_turns_is_default: bool
    fallback_models: tuple[str, ...] = ()


# --- Cursor codec (opaque; carries the boundary row id) ---------------------

# A short, stable prefix so a decoded payload is recognisably one of ours; an
# arbitrary base64 string that happens to decode to a uuid is still rejected.
_CURSOR_PREFIX = "mbr:"


def _encode_cursor(user_id: UUID) -> str:
    """Encode a boundary member id as an opaque URL-safe cursor.

    The wire treats the cursor as opaque (contract ``Cursor`` parameter); only
    the id travels — the query resolves the boundary's ``created_at`` in-database.
    """
    raw = f"{_CURSOR_PREFIX}{user_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary member id.

    Raises:
        ValidationError: the cursor is not one this server issued (malformed
            base64, missing prefix, or non-uuid payload). Fail-closed → 422
            rather than silently returning the first page (INV-8).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    """Clamp the requested page size into the contract's [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _map_logo_validation_error(exc: ValidationError) -> Exception:
    """Re-map a storage logo-validation error to the contract's HTTP status.

    ``ObjectStore.put_logo`` reuses the single ``validate_upload`` allowlist +
    size-cap rule owner, which raises a generic ``ValidationError`` (422) with a
    stable ``code``. The branding contract pins distinct statuses for the upload
    negatives, so map by code here: ``upload_too_large`` → 413,
    ``content_type_not_allowed`` → 415; an empty/other rejection stays 422 (INV-8).
    """
    if exc.code == "upload_too_large":
        return PayloadTooLargeError(exc.detail, code=exc.code)
    if exc.code == "content_type_not_allowed":
        return UnsupportedMediaTypeError(exc.detail, code=exc.code)
    return exc


class _TenantMemberQuery:
    """Tenant-scoped, keyset-paginated user listing for the admin roster.

    See the module docstring's boundary note: this localizes a read that belongs
    on ``db/UserRepository`` once that wave-0 file is next opened. It is held to
    the same discipline as the ``db/`` repositories — the ``tenant_id`` predicate
    is applied to **every** query (INV-1, structural; no method omits it) and it
    returns domain :class:`User` values, never an ORM row or a ``Session``.
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @staticmethod
    def _to_user(row: models.User) -> User:
        return User(
            id=row.id,
            tenant_id=row.tenant_id,
            email=row.email,
            password_hash=row.password_hash,
            roles=tuple(Role(r) for r in row.roles),
            created_at=row.created_at,
            updated_at=row.updated_at,
            email_attested_at=row.email_attested_at,
            email_attested_by=row.email_attested_by,
        )

    async def list_for_tenant_page(self, *, limit: int, after_id: UUID | None = None) -> list[User]:
        """A keyset page of the tenant's members (oldest first, stable order).

        Tenant-scoped (INV-1): only this tenant's users. Ordered by
        ``(created_at, id)`` **ascending** with ``id`` the stable tiebreaker, so
        the total order is deterministic even when two rows share a timestamp.
        ``after_id`` is the decoded cursor (the previous page's last id); rows
        strictly after it are returned, capped at ``limit``. The boundary's
        ``created_at`` is resolved by a correlated scalar subquery (exact on
        Postgres and on the offline SQLite — no timestamp crosses the wire),
        mirroring the collections/documents keyset. A foreign cursor id resolves
        to NULL inside this tenant, so the keyset predicate excludes everything —
        fail-closed rather than leaking another tenant's ordering.
        """
        conditions = [models.User.tenant_id == self._tenant_id]
        if after_id is not None:
            boundary_created_at = (
                select(models.User.created_at)
                .where(
                    models.User.tenant_id == self._tenant_id,
                    models.User.id == after_id,
                )
                .scalar_subquery()
            )
            conditions.append(
                or_(
                    models.User.created_at > boundary_created_at,
                    and_(
                        models.User.created_at == boundary_created_at,
                        models.User.id > after_id,
                    ),
                )
            )
        stmt = (
            select(models.User)
            .where(*conditions)
            .order_by(models.User.created_at.asc(), models.User.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_user(r) for r in rows]


class AdminService:
    """Admin console surfaces for one tenant (issues #87, #148).

    Constructed per-request with the session, the resolved ``tenant_id`` (from
    the token, never request input), and ``Settings`` (the model registry +
    default budgets are config). The governance surfaces are reads; the one write
    is :meth:`update_tenant_settings` — a reversible, tenant-scoped **T1** action
    (spec 0004 §2.5), audited here. Role gating (admin-only, INV-5) is the
    router's ``require_roles`` dependency; this service is only reached once that
    gate has passed.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, settings: Settings) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._members = _TenantMemberQuery(session, tenant_id)
        self._settings = settings

    async def list_members(self, *, cursor: str | None, limit: int | None) -> MemberPage:
        """Return one keyset page of the caller's tenant's members.

        Tenant-scoped (INV-1). Fetches ``limit + 1`` rows to decide whether a
        next page exists without a second round-trip; the extra row (if present)
        sets ``next_cursor`` and is then dropped.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._members.list_for_tenant_page(limit=page_size + 1, after_id=after_id)
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return MemberPage(items=page, next_cursor=next_cursor)

    async def attest_member_identity(
        self,
        member_id: UUID,
        *,
        denials: PermissionDeniedContext,
    ) -> User | None:
        """Attest a member's email identity for connector-ACL mapping (ADR-0019 §2).

        The tenant admin's explicit statement that this email is the person it
        names — the prerequisite for the ACL mapper to ever emit this user's
        principal. Idempotent (re-attesting refreshes the timestamp), audited
        ``user.identity_attested`` (INV-6). Returns the updated member, or
        ``None`` for a missing / foreign-tenant id (→ 404, INV-1). Role gating
        is the router's ``require_roles`` dependency, like every /admin route.
        """
        actor_id = denials.require_user()
        attested = await UserRepository(self._session, self._tenant_id).attest_email(
            member_id, attested_by=actor_id
        )
        if attested is None:
            await denials.emit(
                resource_type="user",
                resource_id=str(member_id),
                attempted_action="user.identity.attest",
                reason="not_visible",
            )
            return None
        await AuditSink(AuditEventRepository(self._session, self._tenant_id)).emit(
            action=AuditAction.USER_IDENTITY_ATTESTED,
            actor=denials.actor,
            resource_type="user",
            resource_id=str(member_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=denials.request_id,
            source_ip=denials.source_ip,
            metadata={"basis": "admin_attestation"},
        )
        return attested

    def model_governance(self) -> ModelGovernanceView:
        """Which models are permitted and the governance tiers they map to.

        Drawn from the curated model registry (#47 — config in
        ``core/config.py``) via :class:`ChatModelService`, so adding/removing a
        model is a config change. ``allowed_models`` preserves the registry's
        configured order; ``tiers`` describes exactly the tiers those models map
        to (no orphan tiers, no undescribed tier). Read-only — there is no
        write-back of governance (read-before-write).
        """
        models_ = ChatModelService(self._settings).list_models()
        allowed = [
            ModelGovernanceEntryView(model_id=m.id, tier=m.tier.value, label=m.label)
            for m in models_
        ]
        # Describe each tier referenced by an allowed model, in the canonical
        # ModelTier order so the view is stable across runs.
        referenced = {m.tier for m in models_}
        tiers = [
            GovernanceTierView(id=tier.value, description=_MODEL_TIER_DESCRIPTIONS[tier])
            for tier in ModelTier
            if tier in referenced
        ]
        return ModelGovernanceView(allowed_models=allowed, tiers=tiers)

    def risk_tiers(self) -> list[RiskTierView]:
        """The read-before-write risk-tier reference (T0–T3, spec 0004 §2.5).

        Static product policy (issue #87: prefer static config, not a table) —
        the same for every tenant. Returned in tier order T0 → T3.
        """
        return list(_RISK_TIERS)

    # --- Per-tenant settings (issue #148) -----------------------------------

    async def get_tenant_settings(self) -> TenantSettingsView:
        """The caller's tenant's admin-configurable settings (issue #148).

        Returns the **effective** chat tool-turn budget — the tenant's override if
        set, else the system default (``Settings.chat_max_tool_turns``) — and
        whether the default is in force. Tenant-scoped (INV-1): the tenant id is
        the one the router resolved from the token.
        """
        tenant = await TenantRepository(self._session).get(self._tenant_id)
        override = tenant.max_tool_turns if tenant is not None else None
        fallbacks = tenant.fallback_models if tenant is not None else None
        return self._settings_view(override, fallbacks)

    async def update_tenant_settings(
        self,
        *,
        max_tool_turns: int | None,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
        fallback_models: list[str] | None = None,
    ) -> TenantSettingsView:
        """Set (or clear) the tenant's chat tool-turn budget override (issue #148).

        The one /admin **write** — a reversible, tenant-scoped **T1** action (spec
        0004 §2.5): admin-gated one layer up (INV-5), audited here (INV-6).
        ``max_tool_turns`` is written as given: an int sets the per-tenant override,
        ``None`` clears it so the system default applies again. The wire schema +
        the DB ``ck_tenants_max_tool_turns_range`` check bound it to 1–50 (INV-8).
        Returns the resulting effective settings (a read-back of what was stored).

        ``fallback_models`` (ADR-0016 §4, #413) is PATCH-shaped: ``None`` leaves
        the stored list unchanged (existing callers keep working); ``[]`` clears
        it; a non-empty list replaces it after validation — each id must pass the
        SAME allow-list check the chat send path applies (config registry or the
        tenant's enabled ``provider:`` models), the list is deduplicated
        preserving order, and its length is bounded (INV-8). An invalid id is a
        422 naming the offender; nothing is written on rejection.
        """
        repo = TenantRepository(self._session)
        stored_fallbacks: list[str] | None
        if fallback_models is None:
            tenant = await repo.get(self._tenant_id)
            stored_fallbacks = tenant.fallback_models if tenant is not None else None
        else:
            stored_fallbacks = await self._validated_fallbacks(fallback_models)
            if (
                await repo.set_fallback_models(self._tenant_id, fallback_models=stored_fallbacks)
                is None
            ):
                raise NotFoundError("Tenant not found.")
        updated = await repo.update(self._tenant_id, max_tool_turns=max_tool_turns)
        if updated is None:
            # Unreachable in practice (the caller's own tenant, resolved from the
            # token, exists); guard so a vanished tenant is a clean 404, not a 500.
            raise NotFoundError("Tenant not found.")
        # Audit the settings write (INV-6 / mission filter #4) — the action ran and
        # is attributed to the admin who made it; the new value rides the metadata.
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.TENANT_SETTINGS_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="tenant",
            resource_id=str(self._tenant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={
                "max_tool_turns": max_tool_turns,
                # Only when this PATCH changed the list (None = untouched).
                **(
                    {"fallback_models": stored_fallbacks or []}
                    if fallback_models is not None
                    else {}
                ),
            },
        )
        return self._settings_view(updated.max_tool_turns, stored_fallbacks)

    # How many fallback models a tenant may chain (INV-8: a bounded, validated
    # configuration — a long chain would multiply worst-case answer latency).
    _MAX_FALLBACK_MODELS = 3

    async def _validated_fallbacks(self, requested: list[str]) -> list[str] | None:
        """Validate + normalise a fallback list; 422 on any invalid entry (#413).

        Each id must pass the same allow-list check as a send-path model (the
        config registry, or a ``provider:`` id owned+enabled by THIS tenant —
        cross-tenant ids fail closed, INV-1/INV-2). Order is preserved,
        duplicates collapse to first occurrence, and an empty result stores
        ``None`` (no fallback).
        """
        deduped: list[str] = []
        for raw in requested:
            model_id = raw.strip()
            if not model_id:
                raise ValidationError("Fallback model ids must be non-empty strings.")
            if model_id not in deduped:
                deduped.append(model_id)
        if len(deduped) > self._MAX_FALLBACK_MODELS:
            raise ValidationError(
                f"At most {self._MAX_FALLBACK_MODELS} fallback models may be configured."
            )
        models_service = ChatModelService(
            self._settings, session=self._session, tenant_id=self._tenant_id
        )
        for model_id in deduped:
            if not await models_service.is_allowed_model_async(model_id):
                raise ValidationError(f"Unknown or unavailable fallback model: {model_id!r}.")
        return deduped or None

    # --- Per-tenant application logo (admin branding) -----------------------

    async def set_tenant_logo(
        self,
        *,
        object_store: ObjectStore,
        logo_bytes: bytes,
        logo_content_type: str,
        logo_filename: str,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> str:
        """Store the tenant's application logo and return a presigned GET URL.

        A reversible, tenant-scoped **T1** action (spec 0004 §2.5): admin-gated one
        layer up (INV-5), audited here (INV-6). The bytes are validated against the
        image-only logo allowlist/limit and stored via ``object_store.put_logo``
        (the only object-store caller); the resulting key is persisted on the
        tenant row so every user of the tenant sees the mark. An over-size logo maps
        to **413** and a non-image to **415** (the branding contract's negatives).
        Returns a short-TTL presigned GET URL the shell renders immediately.
        """
        try:
            stored = await object_store.put_logo(
                tenant_id=str(self._tenant_id),
                data=logo_bytes,
                content_type=logo_content_type,
                filename=logo_filename,
            )
        except ValidationError as exc:
            raise _map_logo_validation_error(exc) from exc

        updated = await TenantRepository(self._session).set_logo_key(
            self._tenant_id, logo_key=stored.key
        )
        if updated is None:
            # Unreachable in practice (the caller's own tenant, resolved from the
            # token, exists); guard so a vanished tenant is a clean 404, not a 500.
            raise NotFoundError("Tenant not found.")

        await self._emit_branding_audit(
            actor_id=actor_id,
            request_id=request_id,
            source_ip=source_ip,
            has_logo=True,
        )
        return await object_store.presign_get(str(self._tenant_id), stored.key)

    async def clear_tenant_logo(
        self,
        *,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> None:
        """Clear the tenant's application logo so the default brand mark applies again.

        The reverse of :meth:`set_tenant_logo` (T1, admin-gated, audited): sets the
        tenant's ``logo_key`` to ``None``. Idempotent — clearing an already-unset
        logo is a no-op mutation that still records the audited action. The stored
        object is left in place (content-addressed, unreferenced); no bytes are read.
        """
        updated = await TenantRepository(self._session).set_logo_key(self._tenant_id, logo_key=None)
        if updated is None:
            raise NotFoundError("Tenant not found.")
        await self._emit_branding_audit(
            actor_id=actor_id,
            request_id=request_id,
            source_ip=source_ip,
            has_logo=False,
        )

    async def _emit_branding_audit(
        self,
        *,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
        has_logo: bool,
    ) -> None:
        """Emit the branding-update audit event (INV-6 / mission filter #4).

        Mirrors ``update_tenant_settings``'s audit: the action ran and is attributed
        to the admin who made it; ``has_logo`` on the metadata records whether a logo
        was set or cleared (the object key itself is not audited — it is derivable and
        not a security-relevant value).
        """
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.TENANT_BRANDING_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="tenant",
            resource_id=str(self._tenant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={"has_logo": has_logo},
        )

    def _settings_view(
        self, override: int | None, fallback_models: list[str] | None = None
    ) -> TenantSettingsView:
        """Project stored overrides into the effective settings view (issue #148, #413)."""
        fallbacks = tuple(fallback_models or ())
        if override is None:
            return TenantSettingsView(
                max_tool_turns=self._settings.chat_max_tool_turns,
                max_tool_turns_is_default=True,
                fallback_models=fallbacks,
            )
        return TenantSettingsView(
            max_tool_turns=override,
            max_tool_turns_is_default=False,
            fallback_models=fallbacks,
        )
