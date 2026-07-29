"""Tool-catalogue route — ``GET /tools`` (issue #505).

The **member-readable, read-only** projection of the per-tenant tool governance the
assistant builder needs to offer a truthful allow-list picker. ``/admin/tool-policy``
already exposes this data, but it is ``admin``-only (INV-5), so an ordinary assistant
owner could not see which tools exist, how risky each one is, or whether their admin
had switched one off. The builder therefore shipped a hardcoded four-entry stub —
which is why ``run_python`` was ungrantable through the UI and the one assistant that
had it was seeded straight into the database.

This endpoint **grants nothing and writes nothing**. It is a read: the registry's
static metadata (``description`` / ``risk_tier`` / ``read_only``) merged with the
tenant's effective ``enabled`` / ``requires_approval`` flags. Every governance
control is untouched — the per-tenant tool policy stays an admin write on
``/admin/tool-policy``, the runner's approval gate still decides whether an allowed
tool may actually run, and the assistant service still validates every allow-list
entry against the registry (an unknown name is a 422). All this does is let the UI
tell the truth about those gates instead of offering a control that silently fails.

Contract-first: the shape matches ``contracts/openapi.yaml`` (``ToolCatalogEntry`` /
``ToolCatalog``). The router validates in → calls **one** service
(:class:`~app.services.tool_policy_service.ToolPolicyService`) → shapes out
(ADR-0004). Bearer-protected via ``current_user`` (a missing/invalid token is a 401,
INV-4) and tenant-scoped via ``current_tenant`` (INV-1) — never request input.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentTenant, CurrentUser, DbSession
from app.services.tool_policy_service import ToolPolicyEntryView, ToolPolicyService

router = APIRouter(tags=["tools"])


# --- Wire models (mirror contracts/openapi.yaml) -----------------------------


class ToolCatalogEntryResponse(BaseModel):
    """``#/components/schemas/ToolCatalogEntry`` — one grantable tool."""

    model_config = {"extra": "forbid"}

    tool_name: str
    description: str
    risk_tier: str
    read_only: bool
    enabled: bool
    requires_approval: bool
    is_default: bool


class ToolCatalogResponse(BaseModel):
    """``#/components/schemas/ToolCatalog`` — every registered tool, in name order."""

    model_config = {"extra": "forbid"}

    items: list[ToolCatalogEntryResponse]


def _to_wire(entries: list[ToolPolicyEntryView]) -> ToolCatalogResponse:
    """Project the service views to the contract wire shape."""
    return ToolCatalogResponse(
        items=[
            ToolCatalogEntryResponse(
                tool_name=entry.tool_name,
                description=entry.description,
                risk_tier=entry.risk_tier,
                read_only=entry.read_only,
                enabled=entry.enabled,
                requires_approval=entry.requires_approval,
                is_default=entry.is_default,
            )
            for entry in entries
        ]
    )


# --- Routes ------------------------------------------------------------------


@router.get("/tools", response_model=ToolCatalogResponse)
async def list_tools(
    _principal: CurrentUser,
    session: DbSession,
    tenant_id: CurrentTenant,
) -> ToolCatalogResponse:
    """Every registered tool with its risk tier and this tenant's effective policy.

    The assistant builder's source for its tool allow-list picker (issue #505): the
    registry is the source of truth for *which* tools exist and their static
    ``risk_tier``/``read_only``/``description``; the tenant's admin overrides (else
    the tool's built-in defaults) supply ``enabled``/``requires_approval`` so the UI
    can show a tool the admin disabled as **unavailable** rather than offering a
    checkbox whose selection would be refused at run time.

    Read-only and grants nothing — the governance write stays admin-only on
    ``PATCH /admin/tool-policy``, and the approval gate is unchanged. Requires a
    valid bearer token (401 otherwise, INV-4); tenant-scoped (INV-1).
    """
    service = ToolPolicyService(session, tenant_id=tenant_id)
    return _to_wire(await service.list_policy())
