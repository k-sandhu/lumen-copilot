"""Admin routes — read-mostly /admin/* governance surfaces (issue #87).

Contract-first: shapes match ``contracts/openapi.yaml`` (``MemberList`` /
``ModelGovernance`` / ``RiskTierList`` and their items). Routers validate in →
call **one** service → shape out (ADR-0004): all orchestration (the members
query + cursor codec, the model-registry projection, the static risk-tier
reference) lives in :class:`~app.services.admin_service.AdminService`; this layer
only (de)serialises.

**Role gating (INV-5).** Every /admin/* route is **admin-only**. The gate is a
*router-level* dependency — ``require_roles(Role.ADMIN)`` (spec 0004 §2.3: role
checks live in ``services/``; this delegates to ``auth_service.require_role``) —
so it runs on **every** path under this router before any handler body. A
non-admin (member or security) gets a **403**; an unauthenticated caller gets a
**401** first (``current_user``, INV-4). The negative test asserts both on every
path.

**Tenancy (INV-1).** ``GET /admin/members`` is scoped to the caller's own tenant
(``current_tenant`` — the token, never request input); another tenant's members
are never returned. Model governance and the risk tiers are tenant-agnostic
reference data (the same for every tenant), so they need only the role gate.

**Read-before-write (spec 0004 §2.5).** The governance surfaces (members, model
governance, risk tiers) are read-only — the admin console reflects governance, it
never changes it. The one write is ``PATCH /admin/settings`` (issue #148): a
reversible, tenant-scoped **T1** action (spec 0004 §2.5 — "authorized owner;
audited; no extra approval") that sets a tenant's chat tool-turn budget. It is
admin-gated like every route here (INV-5) and audited in the service (INV-6); an
out-of-range value is a **422** (INV-8). No T2+ governance mutation exists.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import (
    CurrentTenant,
    CurrentUser,
    DbSession,
    SettingsDep,
    extract_request_id,
    require_roles,
)
from app.domain.entities import Role
from app.services.admin_service import (
    AdminService,
    MemberPage,
    ModelGovernanceView,
    RiskTierView,
    TenantSettingsView,
)
from app.services.sandbox_policy_service import SandboxPolicyService, SandboxPolicyView
from app.services.tool_policy_service import ToolPolicyEntryView, ToolPolicyService

# The admin-only gate runs for every route on this router (INV-5). It depends on
# ``current_user`` underneath, so an unauthenticated caller is a 401 (INV-4)
# before the role check, and a wrong-role caller is a 403.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles(Role.ADMIN))],
)


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class MemberResponse(BaseModel):
    """``#/components/schemas/Member`` — one roster entry."""

    model_config = {"extra": "forbid"}

    id: UUID
    email: str
    role: list[str]


class MemberListResponse(BaseModel):
    """``#/components/schemas/MemberList`` — a cursor-paginated page of members."""

    model_config = {"extra": "forbid"}

    items: list[MemberResponse]
    next_cursor: str | None = None


class ModelGovernanceEntryResponse(BaseModel):
    """``#/components/schemas/ModelGovernanceEntry`` — one allowed model + tier."""

    model_config = {"extra": "forbid"}

    model_id: str
    tier: str
    label: str | None = None


class GovernanceTierResponse(BaseModel):
    """One governance tier (``ModelGovernance.tiers[]`` — id + description)."""

    model_config = {"extra": "forbid"}

    id: str
    description: str


class ModelGovernanceResponse(BaseModel):
    """``#/components/schemas/ModelGovernance`` — allowed models + their tiers."""

    model_config = {"extra": "forbid"}

    allowed_models: list[ModelGovernanceEntryResponse]
    tiers: list[GovernanceTierResponse]


class RiskTierResponse(BaseModel):
    """``#/components/schemas/RiskTier`` — one read-before-write risk tier."""

    model_config = {"extra": "forbid"}

    tier: str
    description: str
    approval: str


class RiskTierListResponse(BaseModel):
    """``#/components/schemas/RiskTierList`` — the T0–T3 reference."""

    model_config = {"extra": "forbid"}

    items: list[RiskTierResponse]


class TenantSettingsResponse(BaseModel):
    """``#/components/schemas/TenantSettings`` — the per-tenant admin settings."""

    model_config = {"extra": "forbid"}

    max_tool_turns: int
    max_tool_turns_is_default: bool


class TenantSettingsUpdateRequest(BaseModel):
    """``#/components/schemas/TenantSettingsUpdate`` — set/clear the budget override.

    ``max_tool_turns`` is required so the intent is explicit: an int (1–50) sets
    the per-tenant override; ``null`` clears it so the system default applies. An
    out-of-band value is rejected here as a **422** (INV-8) before the service.
    """

    model_config = {"extra": "forbid"}

    max_tool_turns: int | None = Field(ge=1, le=50)


class ToolPolicyEntryResponse(BaseModel):
    """``#/components/schemas/ToolPolicyEntry`` — one tool's effective governance."""

    model_config = {"extra": "forbid"}

    tool_name: str
    risk_tier: str
    read_only: bool
    enabled: bool
    requires_approval: bool
    is_default: bool


class ToolPolicyResponse(BaseModel):
    """``#/components/schemas/ToolPolicy`` — the per-tenant tool-governance policy."""

    model_config = {"extra": "forbid"}

    items: list[ToolPolicyEntryResponse]


class ToolPolicyUpdateRequest(BaseModel):
    """``#/components/schemas/ToolPolicyUpdate`` — set one tool's per-tenant flags.

    Both flags are required so the stored override is explicit; the tool name is
    validated against the registry in the service (an unknown tool → 422, INV-8).
    """

    model_config = {"extra": "forbid"}

    tool_name: str = Field(min_length=1)
    enabled: bool
    requires_approval: bool


class SandboxPolicyResponse(BaseModel):
    """``#/components/schemas/SandboxPolicy`` — the effective per-tenant sandbox policy."""

    model_config = {"extra": "forbid"}

    enabled: bool
    allowed_packages: list[str]
    denied_packages: list[str]
    egress_allowed: bool
    egress_allowlist: list[str]
    max_runtime_s: int
    max_memory_mb: int
    daily_runtime_cap_s: int
    max_concurrency: int
    is_default: bool
    max_runtime_s_ceiling: int
    max_memory_mb_ceiling: int
    daily_runtime_cap_s_ceiling: int
    max_concurrency_ceiling: int


class SandboxPolicyUpdateRequest(BaseModel):
    """``#/components/schemas/SandboxPolicyUpdate`` — set the per-tenant sandbox policy.

    All fields required so the stored policy is explicit; the caps are positive at the
    wire (a non-positive value → 422, INV-8). The service clamps each cap DOWN to the
    deploy-wide config ceiling and strips the metadata IP from the egress allowlist (a
    per-tenant value can only narrow, never widen).
    """

    model_config = {"extra": "forbid"}

    enabled: bool
    allowed_packages: list[str]
    denied_packages: list[str]
    egress_allowed: bool
    egress_allowlist: list[str]
    max_runtime_s: int = Field(ge=1)
    max_memory_mb: int = Field(ge=1)
    daily_runtime_cap_s: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)


# --- Serialisation helpers --------------------------------------------------


def _to_member_list(page: MemberPage) -> MemberListResponse:
    return MemberListResponse(
        items=[
            MemberResponse(id=user.id, email=user.email, role=[r.value for r in user.roles])
            for user in page.items
        ],
        next_cursor=page.next_cursor,
    )


def _to_governance(view: ModelGovernanceView) -> ModelGovernanceResponse:
    return ModelGovernanceResponse(
        allowed_models=[
            ModelGovernanceEntryResponse(model_id=e.model_id, tier=e.tier, label=e.label)
            for e in view.allowed_models
        ],
        tiers=[GovernanceTierResponse(id=t.id, description=t.description) for t in view.tiers],
    )


def _to_risk_tiers(tiers: list[RiskTierView]) -> RiskTierListResponse:
    return RiskTierListResponse(
        items=[
            RiskTierResponse(tier=t.tier, description=t.description, approval=t.approval)
            for t in tiers
        ]
    )


def _to_tenant_settings(view: TenantSettingsView) -> TenantSettingsResponse:
    return TenantSettingsResponse(
        max_tool_turns=view.max_tool_turns,
        max_tool_turns_is_default=view.max_tool_turns_is_default,
    )


def _to_tool_policy(entries: list[ToolPolicyEntryView]) -> ToolPolicyResponse:
    return ToolPolicyResponse(
        items=[
            ToolPolicyEntryResponse(
                tool_name=e.tool_name,
                risk_tier=e.risk_tier,
                read_only=e.read_only,
                enabled=e.enabled,
                requires_approval=e.requires_approval,
                is_default=e.is_default,
            )
            for e in entries
        ]
    )


def _to_sandbox_policy(view: SandboxPolicyView) -> SandboxPolicyResponse:
    return SandboxPolicyResponse(
        enabled=view.enabled,
        allowed_packages=list(view.allowed_packages),
        denied_packages=list(view.denied_packages),
        egress_allowed=view.egress_allowed,
        egress_allowlist=list(view.egress_allowlist),
        max_runtime_s=view.max_runtime_s,
        max_memory_mb=view.max_memory_mb,
        daily_runtime_cap_s=view.daily_runtime_cap_s,
        max_concurrency=view.max_concurrency,
        is_default=view.is_default,
        max_runtime_s_ceiling=view.max_runtime_s_ceiling,
        max_memory_mb_ceiling=view.max_memory_mb_ceiling,
        daily_runtime_cap_s_ceiling=view.daily_runtime_cap_s_ceiling,
        max_concurrency_ceiling=view.max_concurrency_ceiling,
    )


# --- Routes -----------------------------------------------------------------


@router.get("/members", response_model=MemberListResponse, response_model_exclude_none=True)
async def list_members(
    session: DbSession,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemberListResponse:
    """The caller's tenant's members and their roles (admin only; tenant-scoped).

    Cursor-paginated, stable order. Admin-only via the router gate (INV-5);
    tenant-scoped via ``current_tenant`` (INV-1). A malformed cursor → 422
    (INV-8), raised in the service.
    """
    service = AdminService(session, tenant_id=tenant_id, settings=settings)
    page = await service.list_members(cursor=cursor, limit=limit)
    return _to_member_list(page)


@router.get("/model-governance", response_model=ModelGovernanceResponse)
async def get_model_governance(
    session: DbSession,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> ModelGovernanceResponse:
    """Which models are permitted, by governance tier (admin only).

    Drawn from the curated model registry (#47 — config), read-only. Admin-only
    via the router gate (INV-5).
    """
    service = AdminService(session, tenant_id=tenant_id, settings=settings)
    return _to_governance(service.model_governance())


@router.get("/risk-tiers", response_model=RiskTierListResponse)
async def get_risk_tiers(
    session: DbSession,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> RiskTierListResponse:
    """The read-before-write risk tiers T0–T3 (admin only).

    Static reference data (spec 0004 §2.5), the same for every tenant. Admin-only
    via the router gate (INV-5).
    """
    service = AdminService(session, tenant_id=tenant_id, settings=settings)
    return _to_risk_tiers(service.risk_tiers())


@router.get("/settings", response_model=TenantSettingsResponse)
async def get_tenant_settings(
    session: DbSession,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> TenantSettingsResponse:
    """The caller's tenant's admin-configurable settings (admin only; tenant-scoped).

    The effective chat tool-turn budget — the tenant's override if set, else the
    system default — plus whether the default is in force (issue #148). Admin-only
    via the router gate (INV-5); tenant-scoped via ``current_tenant`` (INV-1).
    """
    service = AdminService(session, tenant_id=tenant_id, settings=settings)
    return _to_tenant_settings(await service.get_tenant_settings())


@router.patch("/settings", response_model=TenantSettingsResponse)
async def update_tenant_settings(
    body: TenantSettingsUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> TenantSettingsResponse:
    """Set or clear the tenant's chat tool-turn budget (admin only; T1, audited).

    The one /admin write (issue #148): an int (1–50) sets the per-tenant override,
    ``null`` clears it (system default). Admin-only via the router gate (INV-5);
    tenant-scoped via ``current_tenant`` (INV-1); the service audits the change
    (INV-6). An out-of-range value is a **422** at the wire model (INV-8).
    """
    service = AdminService(session, tenant_id=tenant_id, settings=settings)
    view = await service.update_tenant_settings(
        max_tool_turns=body.max_tool_turns,
        actor_id=principal.user_id,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    await session.commit()
    return _to_tenant_settings(view)


@router.get("/tool-policy", response_model=ToolPolicyResponse)
async def get_tool_policy(
    session: DbSession,
    tenant_id: CurrentTenant,
) -> ToolPolicyResponse:
    """The caller's tenant's tool-governance policy (admin only; tenant-scoped).

    For every registered tool: its static risk tier plus the effective per-tenant
    ``enabled`` / ``requires_approval`` flags (an admin override, else the tool's
    built-in default — deny-by-default for a ``requires_approval`` tool). Admin-only
    via the router gate (INV-5); tenant-scoped via ``current_tenant`` (INV-1).
    """
    service = ToolPolicyService(session, tenant_id=tenant_id)
    return _to_tool_policy(await service.list_policy())


@router.patch("/tool-policy", response_model=ToolPolicyResponse)
async def update_tool_policy(
    body: ToolPolicyUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> ToolPolicyResponse:
    """Set the per-tenant ``enabled`` / ``requires_approval`` flags for one tool.

    A reversible, tenant-scoped **T1** governance write (issue #223): admin-only via
    the router gate (INV-5); tenant-scoped via ``current_tenant`` (INV-1); the
    service audits ``tool_policy.updated`` (INV-6) and rejects an unknown tool name
    as **422** (INV-8). Setting a gated tool's ``requires_approval`` to ``false``
    (with ``enabled`` true) is the admin pre-approval that lets the policy-driven
    approval gate execute it (the ``run_python`` unlock). Returns the full policy.
    """
    service = ToolPolicyService(session, tenant_id=tenant_id)
    entries = await service.set_policy(
        tool_name=body.tool_name,
        enabled=body.enabled,
        requires_approval=body.requires_approval,
        actor_id=principal.user_id,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    await session.commit()
    return _to_tool_policy(entries)


@router.get("/sandbox-policy", response_model=SandboxPolicyResponse)
async def get_sandbox_policy(
    session: DbSession,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> SandboxPolicyResponse:
    """The caller's tenant's effective code-execution sandbox policy (admin only; #233).

    Whether code execution is enabled for the tenant, the package allow/deny lists, the
    egress posture, and the runtime / memory / quota caps — every value already clamped
    to the deploy-wide ``SANDBOX_*`` ceiling (a per-tenant policy can only narrow). No
    stored policy ⇒ deny-by-default (code exec off, caps = config ceiling,
    ``is_default=true``). Admin-only via the router gate (INV-5); tenant-scoped via
    ``current_tenant`` (INV-1).
    """
    service = SandboxPolicyService(session, tenant_id=tenant_id, settings=settings)
    return _to_sandbox_policy(await service.get_policy())


@router.patch("/sandbox-policy", response_model=SandboxPolicyResponse)
async def update_sandbox_policy(
    body: SandboxPolicyUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> SandboxPolicyResponse:
    """Set the per-tenant code-execution sandbox policy (admin only; T1, audited; #233).

    Enable/disable code execution, the package allow/deny lists, the egress posture, and
    the runtime / memory / quota caps. A reversible, tenant-scoped **T1** governance
    write: admin-only via the router gate (INV-5); tenant-scoped via ``current_tenant``
    (INV-1); the service audits ``sandbox_policy.updated`` (INV-6). The write can only
    NARROW the config ceiling — a cap above the ceiling is clamped down, and the
    metadata IP is stripped from the egress allowlist (never reachable, G4). A
    non-positive cap is **422** at the wire model (INV-8). Returns the effective policy.
    """
    service = SandboxPolicyService(session, tenant_id=tenant_id, settings=settings)
    view = await service.set_policy(
        enabled=body.enabled,
        allowed_packages=tuple(body.allowed_packages),
        denied_packages=tuple(body.denied_packages),
        egress_allowed=body.egress_allowed,
        egress_allowlist=tuple(body.egress_allowlist),
        max_runtime_s=body.max_runtime_s,
        max_memory_mb=body.max_memory_mb,
        daily_runtime_cap_s=body.daily_runtime_cap_s,
        max_concurrency=body.max_concurrency,
        actor_id=principal.user_id,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    await session.commit()
    return _to_sandbox_policy(view)
