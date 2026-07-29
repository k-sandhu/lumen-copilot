"""Admin tool-governance use-cases (issue #223) — the per-tenant tool policy.

The orchestration behind the ``/admin/tool-policy`` GET/PATCH surface (ADR-0004:
``services/`` composes adapters; the router calls exactly one service). It reads
and writes the per-tenant **tool policy** — for each registered tool, whether it
is ``enabled`` for the tenant and whether an invocation still
``requires_approval`` — the policy the runner's :class:`PolicyApprovalGate`
consults at invoke time. This is the unlock for ``run_python`` (#231) and any
write-tier tool: a ``requires_approval`` tool stays denied until an admin here
enables AND pre-approves it (``enabled=true`` + ``requires_approval=false``).

Deny-by-default (spec 0004 §2.5 / mission filter #3): the source of truth is the
tool **registry** (the built-in defaults), overlaid with the tenant's stored
overrides. A tool with no override row shows its built-in default flags (a
``requires_approval`` tool is off until an admin acts) and is marked
``is_default=True``. The write validates ``tool_name`` against the registry — an
unknown tool is a **422** (INV-8), so no free-text tool can be stored.

Role gating (admin-only, INV-5) is enforced one layer up by the ``/admin`` router's
``require_roles(Role.ADMIN)`` dependency; a member/non-admin never reaches this
service (403). Tenant binding (spec 0004 §2.3): the tenant id is the one the
router resolved from the caller's token, never request input (INV-1). The write is
a reversible, tenant-scoped **T1** governance action, audited here
(``tool_policy.updated``, INV-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.db.repositories import AuditEventRepository, TenantToolPolicyRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, TenantToolPolicy
from app.services.audit import AuditSink
from app.services.tools.registry import all_tools, has_tool


@dataclass(frozen=True, slots=True)
class ToolPolicyEntryView:
    """The effective governance for one registered tool in this tenant (issue #223).

    ``description`` / ``risk_tier`` / ``read_only`` are the tool's static registry
    metadata (for display). ``enabled`` / ``requires_approval`` are the effective
    flags — the admin override if one exists, else the tool's built-in default.
    ``is_default`` says whether no override exists (so the UI can show "default"). A
    gated tool executes only when ``enabled and not requires_approval`` (the admin
    pre-approved it); otherwise the approval gate denies it (deny-by-default).

    ``description`` is the registry's model-facing summary; the member-readable
    ``GET /tools`` catalogue (issue #505) surfaces it so the assistant builder can
    label a tool honestly instead of carrying a hardcoded stub. The admin
    ``/admin/tool-policy`` response does not include it (that surface is a
    governance table, not a picker).
    """

    tool_name: str
    risk_tier: str
    read_only: bool
    enabled: bool
    requires_approval: bool
    is_default: bool
    description: str = ""


class ToolPolicyService:
    """Read/write the per-tenant tool-governance policy for one tenant (issue #223).

    Constructed per-request with the session and the resolved ``tenant_id`` (from
    the token, never request input). Role gating (admin-only, INV-5) is the
    ``/admin`` router's ``require_roles`` dependency; this service is only reached
    once that gate has passed. The read merges the registry defaults with the
    tenant's stored overrides; the write validates the tool name against the
    registry, upserts the override, and audits it.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._policies = TenantToolPolicyRepository(session, tenant_id)

    async def list_policy(self) -> list[ToolPolicyEntryView]:
        """The full per-tenant tool policy: one entry per registered tool.

        The registry is the source of truth for *which* tools exist and their
        built-in defaults; the tenant's stored overrides take precedence where
        present. Deterministically ordered by tool name (``all_tools`` is sorted).
        Tenant-scoped (INV-1).
        """
        overrides = {p.tool_name: p for p in await self._policies.list_all()}
        return [self._entry(row, overrides) for row in _registry_rows()]

    async def set_policy(
        self,
        *,
        tool_name: str,
        enabled: bool,
        requires_approval: bool,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> list[ToolPolicyEntryView]:
        """Set the tenant's per-tenant flags for one registered tool (issue #223).

        A reversible, tenant-scoped **T1** governance write: validates ``tool_name``
        against the registry (an unknown tool is a **422**, INV-8 — no free-text
        tool can be stored), upserts the override, and audits ``tool_policy.updated``
        (INV-6). Turning a gated tool's ``requires_approval`` to ``False`` while
        ``enabled`` is the admin pre-approval that lets the approval gate execute it
        (the ``run_python`` unlock). Returns the full resulting policy (a read-back).
        """
        if not has_tool(tool_name):
            # Deny-by-default: an unknown tool name cannot be stored (INV-8).
            raise ValidationError(f"Unknown tool: {tool_name!r}.", code="unknown_tool")
        await self._policies.upsert(
            tool_name=tool_name,
            enabled=enabled,
            requires_approval=requires_approval,
            updated_by=actor_id,
        )
        # Audit the governance write (INV-6 / mission filter #4) — who set which
        # tool's flags to what, so the switch the approval gate consults is
        # attributable after the fact.
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.TOOL_POLICY_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="tool",
            resource_id=tool_name,
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={
                "tool_name": tool_name,
                "enabled": enabled,
                "requires_approval": requires_approval,
            },
        )
        return await self.list_policy()

    def _entry(
        self,
        row: _RegistryRow,
        overrides: dict[str, TenantToolPolicy],
    ) -> ToolPolicyEntryView:
        """Project one tool + its optional override into the effective view.

        No override ⇒ the tool's built-in defaults (a read-only tool is enabled by
        default; a gated tool keeps its ``requires_approval`` default, i.e. denied),
        marked ``is_default=True``. An override wins on both flags.
        """
        override = overrides.get(row.tool_name)
        if override is None:
            return ToolPolicyEntryView(
                tool_name=row.tool_name,
                risk_tier=row.risk_tier,
                read_only=row.read_only,
                # Built-in default: a tool is offered/enabled by default; whether it
                # still needs approval is its registry ``requires_approval`` flag.
                enabled=True,
                requires_approval=row.requires_approval,
                is_default=True,
                description=row.description,
            )
        return ToolPolicyEntryView(
            tool_name=row.tool_name,
            risk_tier=row.risk_tier,
            read_only=row.read_only,
            enabled=override.enabled,
            requires_approval=override.requires_approval,
            is_default=False,
            description=row.description,
        )


@dataclass(frozen=True, slots=True)
class _RegistryRow:
    """One tool's static registry metadata, free of the adapter-bound definition."""

    tool_name: str
    risk_tier: str
    read_only: bool
    requires_approval: bool
    description: str


def _registry_rows() -> list[_RegistryRow]:
    """The registry's static per-tool metadata rows, sorted by tool name.

    Localizes the registry read so :meth:`ToolPolicyService.list_policy` iterates a
    plain record rather than the adapter-bound ``ToolDefinition`` — the service stays
    free of the tool-platform's handler/schema surface.
    """
    return [
        _RegistryRow(
            tool_name=defn.name,
            risk_tier=defn.risk_tier.value,
            read_only=defn.read_only,
            requires_approval=defn.requires_approval,
            description=defn.description,
        )
        for defn in all_tools()
    ]


__all__ = ["ToolPolicyEntryView", "ToolPolicyService"]
