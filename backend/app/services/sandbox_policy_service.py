"""Admin sandbox-governance use-cases (issue #233) — the per-tenant sandbox policy.

The orchestration behind the ``/admin/sandbox-policy`` GET/PATCH surface (ADR-0004:
``services/`` composes adapters; the router calls exactly one service). It reads and
writes the per-tenant **sandbox policy** for the code-execution sandbox (#230/#231):
whether code execution is ``enabled`` for the tenant, the package allow/deny lists,
the outbound egress posture (``egress_allowed`` + ``egress_allowlist``), and the
runtime / memory / quota caps. This is the policy the sandbox admission path
(``sandbox/service.py``) consults per run.

**Deny-by-default / fail-closed (spec 0004 §2.5, ADR-0013 §6, mission filter #3).**
The source of truth is the tenant's stored policy row overlaid on the deploy-wide
``SANDBOX_*`` config CEILING. A tenant with **no stored row** has code execution
DISABLED (matching #230's default-off), and the reported caps are the config ceiling.
The stored caps are ceilings a per-tenant policy can only NARROW, never widen: a
per-tenant ``max_runtime_s`` above the config cap is clamped down, and the cloud
metadata IP is stripped from any egress allowlist (never reachable, G4). This
clamping is applied on **both** the write (what is stored) and the read (what is
reported), so a config ceiling that later tightens re-clamps a previously-stored
policy without a migration.

Role gating (admin-only, INV-5) is enforced one layer up by the ``/admin`` router's
``require_roles(Role.ADMIN)`` dependency; a member/non-admin never reaches this
service (403). Tenant binding (spec 0004 §2.3): the tenant id is the one the router
resolved from the caller's token, never request input (INV-1). The write is a
reversible, tenant-scoped **T1** governance action, audited here
(``sandbox_policy.updated``, INV-6). An out-of-range cap (non-positive) is a **422**
(INV-8), so no unbounded value can be stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.db.repositories import AuditEventRepository, TenantSandboxPolicyRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.code_execution import (
    SANDBOX_REASON_DEPLOY_DISABLED,
    SANDBOX_REASON_POLICY_ABSENT,
    SANDBOX_REASON_POLICY_UNREADABLE,
    SANDBOX_REASON_TENANT_DISABLED,
)
from app.domain.entities import AuditOutcome, TenantSandboxPolicy
from app.services.audit import AuditSink

# The cloud-metadata IP an SSRF/escape attempt targets for credentials. It is **never**
# egress-allowlistable (G4) — the policy strips it from any allowlist. Duplicated here
# (matching ``app.sandbox.spec.METADATA_IP``) deliberately: this ``services/`` module
# must not import the ``app.sandbox`` package (a services→sandbox→services import cycle),
# and the constant is a stable domain value. ``spec.EgressPolicy`` strips it too, so
# even the enforcement path is belt-and-braces.
METADATA_IP = "169.254.169.254"


@dataclass(frozen=True, slots=True)
class SandboxPolicyView:
    """The EFFECTIVE per-tenant sandbox policy for the admin console (issue #233).

    Every cap is already clamped to the deploy-wide config ceiling (a per-tenant value
    only narrows), and the metadata IP is stripped from ``egress_allowlist`` (G4).
    ``is_default`` is True when no per-tenant row exists — code execution is off and
    every cap is the config ceiling (deny-by-default). The ``*_ceiling`` fields report
    the deploy-wide caps so the console can show how far a setting may go.
    """

    enabled: bool
    allowed_packages: tuple[str, ...]
    denied_packages: tuple[str, ...]
    egress_allowed: bool
    egress_allowlist: tuple[str, ...]
    max_runtime_s: int
    max_memory_mb: int
    daily_runtime_cap_s: int
    max_concurrency: int
    is_default: bool
    max_runtime_s_ceiling: int
    max_memory_mb_ceiling: int
    daily_runtime_cap_s_ceiling: int
    max_concurrency_ceiling: int


@dataclass(frozen=True, slots=True)
class EffectiveSandboxPolicy:
    """The clamped policy the sandbox admission path consults per run (issue #233).

    The enforcement view (distinct from the admin :class:`SandboxPolicyView`, which
    also carries the display-only ceilings): a run is admitted only if ``enabled`` is
    True, within the quotas, with egress/packages per this policy ∩ config ceiling.
    ``enabled`` is False when no per-tenant row exists (deny-by-default) OR the
    deploy-wide kill-switch (``SANDBOX_ENABLED``) is off — either alone denies.

    ``disabled_reason`` (issue #502) says WHICH of those refused — the deploy
    kill-switch, an absent tenant row, an explicitly disabled one, or a policy that
    could not be read — because each needs a different person to fix it, and a run
    blocked by one of them used to be indistinguishable from the others. It is
    ``None`` exactly when ``enabled`` is True. Values are
    ``domain.code_execution.SANDBOX_REASON_*``.
    """

    enabled: bool
    allowed_packages: tuple[str, ...]
    denied_packages: tuple[str, ...]
    egress_allowed: bool
    egress_allowlist: tuple[str, ...]
    max_runtime_s: int
    max_memory_mb: int
    daily_runtime_cap_s: int
    max_concurrency: int
    disabled_reason: str | None = None


def _config_ceiling(settings: Settings) -> tuple[int, int, int, int]:
    """The deploy-wide (runtime_s, memory_mb, daily_s, concurrency) ceilings from config.

    Localises the mapping from the ``SANDBOX_*`` config to the four per-tenant caps a
    policy may only narrow. Memory is reported in MiB (the admin unit); the config
    stores bytes.
    """
    return (
        settings.sandbox_wall_clock_seconds,
        settings.sandbox_memory_bytes // (1024 * 1024),
        settings.sandbox_daily_runtime_seconds_per_tenant,
        settings.sandbox_max_concurrent_per_tenant,
    )


def _strip_metadata_ip(targets: tuple[str, ...]) -> tuple[str, ...]:
    """Drop any egress target that references the cloud metadata IP (G4, never reachable).

    Mirrors :class:`app.sandbox.spec.EgressPolicy`'s own stripping so an admin who
    lists the metadata IP cannot make it reachable — the one control that can never be
    widened, applied on both the write and the read.
    """
    return tuple(t for t in targets if METADATA_IP not in t)


def _clamp_view(policy: TenantSandboxPolicy | None, settings: Settings) -> SandboxPolicyView:
    """Project a stored policy (or its absence) into the effective admin view.

    No row ⇒ deny-by-default: code execution off, every cap the config ceiling,
    ``is_default=True``. A stored row is clamped to the ceiling (caps ``min``'d down,
    the metadata IP stripped from egress). Applied on the read so a later-tightened
    config re-clamps without a migration.
    """
    runtime_ceil, memory_ceil, daily_ceil, conc_ceil = _config_ceiling(settings)
    if policy is None:
        return SandboxPolicyView(
            enabled=False,
            allowed_packages=(),
            denied_packages=(),
            egress_allowed=False,
            egress_allowlist=(),
            max_runtime_s=runtime_ceil,
            max_memory_mb=memory_ceil,
            daily_runtime_cap_s=daily_ceil,
            max_concurrency=conc_ceil,
            is_default=True,
            max_runtime_s_ceiling=runtime_ceil,
            max_memory_mb_ceiling=memory_ceil,
            daily_runtime_cap_s_ceiling=daily_ceil,
            max_concurrency_ceiling=conc_ceil,
        )
    return SandboxPolicyView(
        enabled=policy.enabled,
        allowed_packages=policy.allowed_packages,
        denied_packages=policy.denied_packages,
        egress_allowed=policy.egress_allowed,
        # Strip the metadata IP on the read too — belt and braces, never reachable.
        egress_allowlist=_strip_metadata_ip(policy.egress_allowlist),
        # Clamp DOWN to the config ceiling — a per-tenant cap can only narrow.
        max_runtime_s=min(policy.max_runtime_s, runtime_ceil),
        max_memory_mb=min(policy.max_memory_mb, memory_ceil),
        daily_runtime_cap_s=min(policy.daily_runtime_cap_s, daily_ceil),
        max_concurrency=min(policy.max_concurrency, conc_ceil),
        is_default=False,
        max_runtime_s_ceiling=runtime_ceil,
        max_memory_mb_ceiling=memory_ceil,
        daily_runtime_cap_s_ceiling=daily_ceil,
        max_concurrency_ceiling=conc_ceil,
    )


class SandboxPolicyService:
    """Read/write the per-tenant sandbox-governance policy for one tenant (issue #233).

    Constructed per-request with the session, the resolved ``tenant_id`` (from the
    token, never request input), and the deploy-wide ``settings`` (the config ceiling).
    Role gating (admin-only, INV-5) is the ``/admin`` router's ``require_roles``
    dependency; this service is only reached once that gate has passed. The read
    projects the stored row (or its absence) into the effective, clamped view; the
    write validates + clamps the caps, strips the metadata IP, upserts, and audits.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, settings: Settings) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._policies = TenantSandboxPolicyRepository(session, tenant_id)

    async def get_policy(self) -> SandboxPolicyView:
        """The tenant's effective sandbox policy — clamped to the config ceiling.

        No stored row ⇒ deny-by-default (code exec off, caps = config ceiling,
        ``is_default=True``). Tenant-scoped (INV-1).
        """
        return _clamp_view(await self._policies.get(), self._settings)

    async def set_policy(
        self,
        *,
        enabled: bool,
        allowed_packages: tuple[str, ...],
        denied_packages: tuple[str, ...],
        egress_allowed: bool,
        egress_allowlist: tuple[str, ...],
        max_runtime_s: int,
        max_memory_mb: int,
        daily_runtime_cap_s: int,
        max_concurrency: int,
        actor_id: UUID,
        request_id: str,
        source_ip: str,
    ) -> SandboxPolicyView:
        """Set the tenant's sandbox policy (issue #233) — a reversible T1 write.

        Validates the caps are positive (a non-positive value is **422**, INV-8 — no
        unbounded cap can be stored), CLAMPS each cap DOWN to the deploy-wide config
        ceiling (a per-tenant value can only narrow), STRIPS the metadata IP from the
        egress allowlist (never reachable, G4), upserts the per-tenant singleton, and
        audits ``sandbox_policy.updated`` (INV-6). Returns the effective, clamped view
        (a read-back). Changes take effect on subsequent runs.
        """
        # --- Validate: a non-positive cap would disable an isolation control (INV-8).
        self._require_positive("max_runtime_s", max_runtime_s)
        self._require_positive("max_memory_mb", max_memory_mb)
        self._require_positive("daily_runtime_cap_s", daily_runtime_cap_s)
        self._require_positive("max_concurrency", max_concurrency)

        runtime_ceil, memory_ceil, daily_ceil, conc_ceil = _config_ceiling(self._settings)
        # Clamp DOWN to the ceiling + strip the metadata IP BEFORE storing — the stored
        # row can never widen past the config, so even a direct read of the row is safe.
        stored = await self._policies.upsert(
            enabled=enabled,
            allowed_packages=allowed_packages,
            denied_packages=denied_packages,
            egress_allowed=egress_allowed,
            egress_allowlist=_strip_metadata_ip(egress_allowlist),
            max_runtime_s=min(max_runtime_s, runtime_ceil),
            max_memory_mb=min(max_memory_mb, memory_ceil),
            daily_runtime_cap_s=min(daily_runtime_cap_s, daily_ceil),
            max_concurrency=min(max_concurrency, conc_ceil),
            updated_by=actor_id,
        )
        # Audit the governance write (INV-6 / mission filter #4) — who set the tenant's
        # sandbox policy to what, so the policy the admission path consults is
        # attributable after the fact. Never log package/egress *values* beyond the
        # summary flags — the enable + egress posture is the security-relevant switch.
        audit = AuditSink(AuditEventRepository(self._session, self._tenant_id))
        await audit.emit(
            action=AuditAction.SANDBOX_POLICY_UPDATED,
            actor=AuditActor.user(actor_id),
            resource_type="tenant",
            resource_id=str(self._tenant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=request_id,
            source_ip=source_ip,
            metadata={
                "enabled": stored.enabled,
                "egress_allowed": stored.egress_allowed,
                "allowed_package_count": len(stored.allowed_packages),
                "denied_package_count": len(stored.denied_packages),
                "egress_allowlist_count": len(stored.egress_allowlist),
                "max_runtime_s": stored.max_runtime_s,
                "max_memory_mb": stored.max_memory_mb,
                "daily_runtime_cap_s": stored.daily_runtime_cap_s,
                "max_concurrency": stored.max_concurrency,
            },
        )
        return _clamp_view(stored, self._settings)

    @staticmethod
    def _require_positive(field: str, value: int) -> None:
        """Reject a non-positive cap (INV-8): it would disable an isolation control."""
        if value <= 0:
            raise ValidationError(
                f"{field} must be a positive integer.", code="invalid_sandbox_policy"
            )


class SandboxPolicyReader:
    """Resolve the effective sandbox policy the admission path consults (issue #233).

    Constructed per-run with the runtime's own DB session, the running tenant id (from
    the principal, never request input), and the deploy-wide ``settings``. **Fail-closed
    (INV-7):** any error reading the policy resolves to a DISABLED policy — a run is
    refused rather than admitted on a policy that could not be read. The deploy-wide
    kill-switch (``SANDBOX_ENABLED``) is AND-ed in: with it off, no tenant is enabled
    regardless of a stored row.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, settings: Settings) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings

    async def resolve(self) -> EffectiveSandboxPolicy:
        """The tenant's effective, clamped policy for a run — deny-by-default, fail-closed.

        ``enabled`` is True only if the deploy-wide kill-switch is on AND a stored
        per-tenant row has ``enabled=True``. No row / a read error ⇒ disabled (the run
        is denied before the runner). Caps are clamped to the config ceiling; the
        metadata IP is stripped from egress (never reachable, G4).

        Every disabled outcome carries the ``disabled_reason`` that caused it
        (issue #502). The deploy kill-switch is checked FIRST and reported first
        because it **dominates**: while it is off, enabling the tenant policy would
        change nothing, so pointing an admin at their own switch would be a wrong
        instruction. (It also spares a pointless DB read on a deploy with code
        execution turned off.)
        """
        if not self._settings.sandbox_enabled:
            return _disabled_effective(self._settings, SANDBOX_REASON_DEPLOY_DISABLED)
        try:
            policy = await TenantSandboxPolicyRepository(self._session, self._tenant_id).get()
        except Exception:  # noqa: BLE001 — a policy we can't read must never admit a run
            # Fail closed (INV-7): refuse rather than run on an unreadable policy.
            return _disabled_effective(self._settings, SANDBOX_REASON_POLICY_UNREADABLE)
        if policy is None:
            # Deny-by-default: no per-tenant policy ⇒ code execution disabled.
            return _disabled_effective(self._settings, SANDBOX_REASON_POLICY_ABSENT)
        runtime_ceil, memory_ceil, daily_ceil, conc_ceil = _config_ceiling(self._settings)
        return EffectiveSandboxPolicy(
            # The deploy-wide kill-switch (checked above) AND the per-tenant enable
            # must BOTH be on; only the per-tenant one can still refuse here.
            enabled=policy.enabled,
            allowed_packages=policy.allowed_packages,
            denied_packages=policy.denied_packages,
            egress_allowed=policy.egress_allowed,
            egress_allowlist=_strip_metadata_ip(policy.egress_allowlist),
            max_runtime_s=min(policy.max_runtime_s, runtime_ceil),
            max_memory_mb=min(policy.max_memory_mb, memory_ceil),
            daily_runtime_cap_s=min(policy.daily_runtime_cap_s, daily_ceil),
            max_concurrency=min(policy.max_concurrency, conc_ceil),
            disabled_reason=None if policy.enabled else SANDBOX_REASON_TENANT_DISABLED,
        )


def _disabled_effective(settings: Settings, reason: str) -> EffectiveSandboxPolicy:
    """The deny-by-default effective policy: disabled, every cap the config ceiling.

    ``reason`` is the ``SANDBOX_REASON_*`` that refused — required, so a new
    disabled path cannot be added without saying which switch it is (#502).
    """
    runtime_ceil, memory_ceil, daily_ceil, conc_ceil = _config_ceiling(settings)
    return EffectiveSandboxPolicy(
        enabled=False,
        allowed_packages=(),
        denied_packages=(),
        egress_allowed=False,
        egress_allowlist=(),
        max_runtime_s=runtime_ceil,
        max_memory_mb=memory_ceil,
        daily_runtime_cap_s=daily_ceil,
        max_concurrency=conc_ceil,
        disabled_reason=reason,
    )


__all__ = [
    "EffectiveSandboxPolicy",
    "SandboxPolicyReader",
    "SandboxPolicyService",
    "SandboxPolicyView",
]
