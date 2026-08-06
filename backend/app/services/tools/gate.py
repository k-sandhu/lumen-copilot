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

**Deny-by-default and fail-closed** (spec 0004 §2.5 / mission filter #3) — and
since #502 every refusal says *which* switch refused
(:class:`~app.services.tools.types.ApprovalDecision`), because the four cases below
need four different fixes:

* No admin row for the tool ⇒ the tool's built-in default applies. A
  ``requires_approval`` tool has no pre-approval, so it is **denied**
  (``tool_policy_absent``) — the same behaviour as the old deny-all gate until an
  admin acts.
* An admin row with ``enabled=false`` ⇒ denied (``tool_policy_disabled``: the
  tenant disabled the tool).
* An admin row with ``enabled=true`` but ``requires_approval=true`` ⇒ denied
  (``approval_required_unavailable``). **This is a permanent deny, not a pause**
  (issue #500): no surface in the product can grant that approval — there is no
  interactive approver in a chat turn, and the interactive flow is #501, still
  unbuilt. So the refusal states plainly that approval is required but cannot be
  obtained here, and that clearing "requires approval" is what unblocks it. It
  must never read as "disabled": the admin *did* enable the tool.
* Any error reading the policy (a DB failure) ⇒ **denied**
  (``tool_policy_unreadable``). The gate never lets a consequential action through
  on a policy it could not read (fail-closed, INV-7). The typed reason records the
  fault for the operator; the ``detail`` the model reads says only "try again
  shortly", because "the policy store is unreachable" is a live infrastructure
  signal and every ``detail`` here is prompt-injection-reachable (see
  ``domain/code_execution`` for the same split on the sandbox side).
* An admin row with ``enabled=true AND requires_approval=false`` ⇒ **allowed** (the
  admin pre-approved it for the tenant).

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
from app.domain.tools import (
    APPROVAL_REASON_APPROVAL_UNAVAILABLE,
    APPROVAL_REASON_POLICY_ABSENT,
    APPROVAL_REASON_POLICY_DISABLED,
    APPROVAL_REASON_POLICY_UNREADABLE,
    APPROVAL_SCOPE_TENANT_PREAPPROVAL,
)
from app.services.tools.types import ApprovalDecision, ApprovalRecord, ApprovalRequest

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

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Decide ``request.tool_name``, naming the switch that refused (#502).

        Approves only when an admin row both enabled AND pre-approved the tool.
        Deny-by-default (no row) and fail-closed (a read error denies). Only
        reached for ``requires_approval`` tools — the runner never calls it for
        T0/T1.
        """
        tool = request.tool_name
        try:
            policy = await TenantToolPolicyRepository(self._session, self._tenant_id).get_by_tool(
                tool
            )
        except Exception:  # noqa: BLE001 — a policy we can't read must never allow a T2+ action
            # Fail closed (INV-7): a consequential action is refused rather than
            # executed on an unreadable policy. Log the error *type* only.
            log.error("tool_policy.gate_read_failed", tool=tool)
            return ApprovalDecision.deny(
                APPROVAL_REASON_POLICY_UNREADABLE,
                (
                    f"Tool {tool!r} could not be checked against this workspace's tool "
                    "policy, so it was refused rather than run unchecked. Try again "
                    "shortly."
                ),
            )
        # Deny-by-default: no admin override ⇒ the tool's built-in default (a
        # requires_approval tool has no pre-approval) ⇒ denied.
        if policy is None:
            return ApprovalDecision.deny(
                APPROVAL_REASON_POLICY_ABSENT,
                (
                    f"Tool {tool!r} is not enabled for this workspace — no tool policy "
                    "grants it. An admin can enable it under Admin → Tool governance."
                ),
            )
        if not policy.enabled:
            return ApprovalDecision.deny(
                APPROVAL_REASON_POLICY_DISABLED,
                (
                    f"Tool {tool!r} is switched off for this workspace. An admin can "
                    "enable it under Admin → Tool governance."
                ),
            )
        if policy.requires_approval:
            # Issue #500 — say the honest thing. The admin enabled the tool but left
            # it flagged "requires approval", and NOTHING in the product can grant
            # that approval: a chat turn has no approver, and the interactive flow is
            # #501 (unbuilt). So this is a permanent refusal, and the message must
            # point at the flag rather than at the enable switch that is already on.
            return ApprovalDecision.deny(
                APPROVAL_REASON_APPROVAL_UNAVAILABLE,
                (
                    f"Tool {tool!r} is enabled but still marked 'requires approval', and "
                    "there is no approver on this surface, so it cannot be approved and "
                    "the call was refused. An admin must clear 'requires approval' for "
                    "it under Admin → Tool governance to pre-approve it."
                ),
            )
        # Pre-approved for the tenant: enabled AND no longer requiring approval.
        #
        # INV-7 admits this as a "recorded approval" only since the spec 0004 §2.5
        # amendment (#518), and only because the approval is genuinely recorded: the
        # admin who set the row, the row itself, and a hash of the arguments the grant
        # covered all travel with the decision into `tool_invocations` and the
        # `tool.invoked` audit event. Before that, an allow carried nothing at all, so
        # "recorded approval" described a boolean.
        #
        # It is still a TENANT-wide grant, not per-invocation review: a prompt-injected
        # call in a pre-approved tenant executes. That residual risk is stated in the
        # amended invariant and is what #501 removes.
        return ApprovalDecision.allow(
            ApprovalRecord(
                scope=APPROVAL_SCOPE_TENANT_PREAPPROVAL,
                policy_id=policy.id,
                # May be None: `updated_by` is SET NULL, so a grant outlives the admin
                # who made it. Recording None is honest; inventing an identity is not.
                approved_by=policy.updated_by,
                arguments_hash=request.arguments_hash,
            )
        )


__all__ = ["PolicyApprovalGate"]
