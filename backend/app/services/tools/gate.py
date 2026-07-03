"""The policy-driven approval gate (issue #223) — the live ``ApprovalGate``.

The real :class:`~app.services.tools.types.ApprovalGate` the runner uses on the
live path, replacing the inert fail-closed
:class:`~app.services.tools.types.DenyAllApprovalGate`. Where ``DenyAllApprovalGate``
denies **every** ``requires_approval`` tool unconditionally, this gate consults the
tenant's admin-set **tool policy** (``tenant_tool_policy``, issue #223): a gated
tool is allowed to execute ONLY if an admin row has pre-approved it for the
tenant — ``enabled=true AND requires_approval=false``. This is the unlock for
``run_python`` (#231) and any write-tier tool: until an admin turns it on, it stays
denied.

**Deny-by-default and fail-closed** (spec 0004 §2.5 / mission filter #3):

* No admin row for the tool ⇒ the tool's built-in default applies. A
  ``requires_approval`` tool has no pre-approval, so it is **denied** — the same
  behaviour as the old deny-all gate until an admin acts.
* An admin row with ``enabled=false`` ⇒ denied (the tenant disabled the tool).
* An admin row with ``enabled=true`` but ``requires_approval=true`` ⇒ denied (the
  admin enabled the tool but has NOT pre-approved it — approval is still required,
  and no in-session reviewer exists yet, so the gate refuses).
* An admin row with ``enabled=true AND requires_approval=false`` ⇒ **allowed** (the
  admin pre-approved it for the tenant).
* Any error reading the policy (a DB failure) ⇒ **denied**. The gate never lets a
  consequential action through on a policy it could not read (fail-closed, INV-7).

The runner only routes ``requires_approval`` (⇒ T2+) tools here; T0/T1 tools never
reach the gate. The gate is tenant-scoped at construction (the tenant resolved from
the running principal, never request input), so it can never consult another
tenant's policy (INV-1).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.repositories import TenantToolPolicyRepository
from app.services.tools.types import ApprovalRequest

log = get_logger(__name__)


class PolicyApprovalGate:
    """Approve a gated tool iff the tenant's admin policy pre-approves it (#223).

    Constructed per answer with the runtime's own DB session and the running
    tenant id (from the principal, never request input). Satisfies the
    :class:`~app.services.tools.types.ApprovalGate` Protocol structurally.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def request(self, request: ApprovalRequest) -> bool:
        """Return True iff an admin row enabled AND pre-approved ``request.tool_name``.

        Deny-by-default (no row) and fail-closed (a read error denies). Only reached
        for ``requires_approval`` tools — the runner never calls it for T0/T1.
        """
        try:
            policy = await TenantToolPolicyRepository(
                self._session, self._tenant_id
            ).get_by_tool(request.tool_name)
        except Exception:  # noqa: BLE001 — a policy we can't read must never allow a T2+ action
            # Fail closed (INV-7): a consequential action is refused rather than
            # executed on an unreadable policy. Log the error *type* only.
            log.error("tool_policy.gate_read_failed", tool=request.tool_name)
            return False
        # Deny-by-default: no admin override ⇒ the tool's built-in default (a
        # requires_approval tool has no pre-approval) ⇒ denied.
        if policy is None:
            return False
        # Pre-approved for the tenant iff enabled AND no longer requiring approval.
        return policy.enabled and not policy.requires_approval


__all__ = ["PolicyApprovalGate"]
