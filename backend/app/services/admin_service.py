"""Admin governance use-cases — read-mostly /admin/* (issue #87).

The orchestration layer behind the read-only admin console surfaces (ADR-0004:
``services/`` compose adapters; routers call exactly one service). All three
surfaces are **reads** — there is intentionally **no write/governance mutation**
here (read-before-write, spec 0004 §2.5 / mission filter #3): the MVP is entirely
T0, so the admin console *reflects* governance, it never changes it.

Three use-cases:

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
from app.core.errors import ValidationError
from app.db import models
from app.domain.entities import Role, User
from app.domain.models import ModelTier
from app.services.models_service import ChatModelService

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
    """Read-only governance surfaces for the admin console (issue #87).

    Constructed per-request with the session, the resolved ``tenant_id`` (from
    the token, never request input), and ``Settings`` (the model registry is
    config). Holds **no write** use-case — read-before-write (spec 0004 §2.5).
    Role gating (admin-only) is the router's ``require_roles`` dependency; this
    service is only reached once that gate has passed.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, settings: Settings) -> None:
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
