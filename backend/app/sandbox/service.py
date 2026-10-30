"""Reusable root-capable sandbox lifecycle and execution service (ADR-0020)."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, DependencyError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CodeRunRepository,
    SandboxSessionRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.code_execution import (
    SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
    SANDBOX_REASON_CONCURRENCY_EXCEEDED,
    SANDBOX_REASON_DAILY_RUNTIME_EXCEEDED,
    SANDBOX_REASON_PACKAGE_DENIED,
    SANDBOX_REASON_RUN_ERROR,
    SANDBOX_REASON_RUNNER_UNAVAILABLE,
    SANDBOX_REASON_SESSION_REQUIRED,
    SANDBOX_REASON_TENANT_DISABLED,
    sandbox_reason_message,
    sandbox_reason_public_message,
)
from app.domain.entities import (
    ArtifactProducedBy,
    AuditOutcome,
    CodeRun,
    CodeRunStatus,
    SandboxSession,
    SandboxSessionStatus,
)
from app.sandbox.accounting import (
    AccountingUnavailable,
    ExecutionAlreadyAdmitted,
    RedisSandboxAccounting,
    SandboxAccounting,
)
from app.sandbox.admission import AdmissionLease
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import EffectiveLimits, RunResult, RunSpec, SandboxSessionSpec, StagedInput
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import (
    AuditSink,
    PermissionDeniedContext,
    PermissionDeniedRecorder,
    audited_resource,
)
from app.services.sandbox_policy_service import EffectiveSandboxPolicy, SandboxPolicyReader
from app.storage import ObjectStore

log = get_logger(__name__)


class PackagePolicyError(ValidationError):
    """A requested package is malformed or not admitted by the tenant policy."""

    code = "sandbox_package_denied"


class SandboxDisabledError(ConflictError):
    """Code execution is off — carrying WHICH switch said so (issue #502).

    A plain :class:`~app.core.errors.ConflictError` loses the reason the policy
    reader resolved, which matters twice: the HTTP surface renders only the public
    ``detail``, and the execution path re-checks enablement a second time (inside
    ``SandboxSessionService.ensure``) where a policy flipped mid-run would otherwise
    be classified as an unexplained crash. ``sandbox_reason`` is the typed code
    ``_failure_reason`` reads back out.
    """

    code = "code_execution_disabled"

    def __init__(self, reason: str) -> None:
        super().__init__(sandbox_reason_public_message(reason), code=self.code)
        self.sandbox_reason = reason


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """A finished code run's terminal status PLUS the typed reason behind it (#502).

    ``CodeRunStatus`` alone cannot say which of the gates refused, and the reason is
    not a column on ``code_runs`` — the refusal's *prose* is (as ``stderr``), which
    is why the chat seam used to re-derive the reason by string-slicing that
    sentence. Returning the code directly is what lets ``run_python`` put the stable
    ``sandbox_disabled_deploy`` / ``sandbox_runner_rejected`` / … into the durable
    ``tool_invocations`` row and the audit metadata.

    ``reason_code`` is ``None`` for a run that reached its terminal on its own
    merits: a success, a non-zero exit, an explicit cancel.
    """

    status: CodeRunStatus
    reason_code: str | None = None


def _failure_reason(exc: BaseException) -> str:
    """Classify a crash on the execution path into a typed reason (issue #502).

    An exception that already knows its own reason says so on ``sandbox_reason``:
    the three runner outcomes (unreachable / refused the request / failed the
    request — ``sandbox/runner.py``) and a policy that flipped between this path's
    two enablement checks (:class:`SandboxDisabledError`). Collapsing all of those
    into one code is exactly what #502 exists to stop, so the typed reason wins.

    A bare :class:`~app.core.errors.DependencyError` from anywhere else is still
    read as the runner being unreachable — that is the only dependency this path
    has — and everything else is the honest "failed unexpectedly".
    """
    reason = getattr(exc, "sandbox_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    if isinstance(exc, DependencyError):
        return SANDBOX_REASON_RUNNER_UNAVAILABLE
    return SANDBOX_REASON_RUN_ERROR


def _policy_names(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        "*" if value.strip() == "*" else canonicalize_name(value.strip())
        for value in values
        if value.strip()
    )


def _preinstalled_index(values: tuple[str, ...]) -> dict[str, Version]:
    """Map canonical distribution name → the version the execution image ships.

    ``values`` is the image's manifest as ``name==version`` pins
    (``Settings.sandbox_preinstalled_packages``, mirroring
    ``sandbox_exec/requirements.txt``). An entry that is not a single ``==`` pin is
    ignored rather than trusted: "already installed" must be a *provable* claim, so
    a fuzzy manifest entry falls through to the ordinary allow-list path.
    """
    index: dict[str, Version] = {}
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            continue
        pins = [spec.version for spec in requirement.specifier if spec.operator == "=="]
        if len(pins) != 1:
            continue
        try:
            index[canonicalize_name(requirement.name)] = Version(pins[0])
        except InvalidVersion:
            continue
    return index


def _satisfied_by_image(requirement: Requirement, shipped: Version | None) -> bool:
    """Whether the execution image ALREADY satisfies this exact requirement.

    Every condition is necessary, because "no install needed" must be provable:

    * the distribution is in the image manifest at a known version;
    * the request carries **no extras and no environment marker** — an extra pulls in
      further distributions the manifest promises nothing about, and a marker decides
      *whether* to install at all; both are pip's job, not ours;
    * the shipped version satisfies the requested specifier — otherwise admitting the
      request would silently run a DIFFERENT version than the one asked for, which is
      worse than refusing it.
    """
    if shipped is None or requirement.extras or requirement.marker is not None:
        return False
    return requirement.specifier.contains(shipped, prereleases=True)


def _install_refusal(name: str, value: str, shipped: Version | None) -> str:
    """The refusal sentence for a package that would need a real install (#504).

    Model- and user-facing (it becomes the run's ``stderr``), so it names the control an
    admin would change and nothing about how the deployment is built. It is the one
    model-reachable refusal that does NOT come from
    :data:`~app.domain.code_execution.SANDBOX_REASON_PUBLIC_MESSAGES`, so it carries that
    table's obligation directly: no env var, no service or process topology, no network
    posture. An earlier version ended "...requires the sandbox service to have outbound
    network access", which told a prompt-injectable surface both that a distinct service
    exists and what its egress posture is; the remedy is the same either way — ask an
    admin — so the detail bought the reader nothing and is kept to the operator log.
    """
    if shipped is not None:
        return (
            f"Package '{name}' is available in the code sandbox image as version "
            f"{shipped}, which does not satisfy '{value}'. Installing a different "
            f"version needs '{name}' on this workspace's allowed-packages list "
            "(Admin → Code execution)."
        )
    return (
        f"Package '{name}' is not installed in the code sandbox image and is not on "
        "this workspace's allowed-packages list. An admin can add it under "
        "Admin → Code execution."
    )


def session_resource_bounds(settings: Settings) -> tuple[float | None, int | None, int | None]:
    """Default-on deployment container ceilings (ADR-0029, Proposed)."""
    if not settings.sandbox_session_limits_enabled:
        return (None, None, None)
    return (settings.sandbox_cpus, settings.sandbox_memory_bytes, settings.sandbox_pids_limit)


def resolve_effective_limits(settings: Settings, policy: EffectiveSandboxPolicy) -> EffectiveLimits:
    """Deployment ceilings narrowed by tenant policy — never widened (#589).

    Tenant-narrowable fields are the ones with a stored tenant value
    (runtime, memory, daily runtime, concurrency); CPU, PID, scratch and
    output bounds are deployment-wide. Every value is ``min``'d against its
    ceiling here as defense in depth, so even a legacy stored row above a
    since-tightened ceiling cannot widen a run. Under the explicit
    unbounded opt-out the container bounds are ``None``; concurrency and
    daily accounting still apply at the tenant-policy values.
    """
    bounded = settings.sandbox_session_limits_enabled

    def _source(value: int, ceiling: int) -> str:
        return "tenant_override" if value < ceiling else "deployment_default"

    return EffectiveLimits(
        cpus=settings.sandbox_cpus if bounded else None,
        memory_bytes=(
            min(policy.max_memory_mb * 1024 * 1024, settings.sandbox_memory_bytes)
            if bounded
            else None
        ),
        pids_limit=settings.sandbox_pids_limit if bounded else None,
        wall_clock_seconds=(min(policy.max_runtime_s, settings.sandbox_wall_clock_seconds)),
        output_bytes_cap=settings.sandbox_output_bytes_cap,
        scratch_bytes=settings.sandbox_scratch_bytes if bounded else None,
        max_concurrency=min(policy.max_concurrency, settings.sandbox_max_concurrent_per_tenant),
        daily_runtime_cap_s=min(
            policy.daily_runtime_cap_s, settings.sandbox_daily_runtime_seconds_per_tenant
        ),
        sources={
            "cpus": "deployment_default",
            "memory_bytes": _source(
                policy.max_memory_mb, settings.sandbox_memory_bytes // (1024 * 1024)
            ),
            "pids_limit": "deployment_default",
            "wall_clock_seconds": _source(
                policy.max_runtime_s, settings.sandbox_wall_clock_seconds
            ),
            "output_bytes_cap": "deployment_default",
            "scratch_bytes": "deployment_default",
            "max_concurrency": _source(
                policy.max_concurrency, settings.sandbox_max_concurrent_per_tenant
            ),
            "daily_runtime_cap_s": _source(
                policy.daily_runtime_cap_s, settings.sandbox_daily_runtime_seconds_per_tenant
            ),
        },
    )


def validate_requested_packages(
    requested: tuple[str, ...],
    *,
    allowed: tuple[str, ...],
    denied: tuple[str, ...],
    preinstalled: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Admit PEP-508 requirements and return the ones that still need INSTALLING.

    Deny-wins over everything, direct URLs are never permitted, and the returned
    tuple is what the runner is asked to fetch and install — which is *not* the same
    as what the run may use (issue #504).

    ``preinstalled`` is the execution image's own manifest. A requirement the image
    already satisfies is **admitted without an install**: it needs no wheel download,
    so it works on a deploy whose runner has no route to PyPI, and it cannot be
    refused merely because the tenant's ``allowed_packages`` is empty — which is the
    deny-by-default state of every new tenant, and the reason every ``packages=[...]``
    request used to fail. Anything else is a real install and still needs a real
    grant: the tenant allow-list AND the runner's outbound network.
    """
    allowed_names = _policy_names(allowed)
    denied_names = _policy_names(denied)
    shipped = _preinstalled_index(preinstalled)
    accepted: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        value = raw.strip()
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise PackagePolicyError(f"Package requirement '{value}' is invalid.") from exc
        name = canonicalize_name(requirement.name)
        if requirement.url is not None:
            raise PackagePolicyError(
                f"Package requirement '{value}' uses a direct URL, which is not permitted."
            )
        if name in denied_names:
            # An explicit denial is an admin decision and outranks the image: a denied
            # distribution is never installed, even though a baked one stays importable.
            raise PackagePolicyError(f"Package '{name}' is not allowed for this tenant.")
        shipped_version = shipped.get(name)
        if _satisfied_by_image(requirement, shipped_version):
            # Already in the image: nothing to fetch, nothing to install, no egress.
            continue
        if "*" not in allowed_names and name not in allowed_names:
            raise PackagePolicyError(_install_refusal(name, value, shipped_version))
        if name not in allowed_names:
            # Admitted only because `*` is set (ADR-0020 §3's allow-all grant), so the
            # MODEL just chose a distribution name off the public index and this deploy
            # fetched it (#509). That is a real posture — some tenants want it — but it
            # should be visible in the ops log rather than inferred from a policy row
            # nobody reads. Deliberately not an audit event: nothing was denied and the
            # run is legitimate; this is telemetry about how permissive the grant is.
            log.warning(
                "sandbox.package_admitted_by_wildcard",
                package=name,
                detail=(
                    "allowed_packages contains '*', so any public distribution name "
                    "the model asks for is fetched and installed"
                ),
            )
        if name in seen:
            continue
        seen.add(name)
        # Canonicalize only the leading distribution name; retain extras/specifier/
        # marker spelling so the audited request is the one pip receives.
        accepted.append(re.sub(r"^[A-Za-z0-9_.-]+", name, value, count=1))
    return tuple(accepted)


class SandboxSessionService:
    """Owner-scoped lifecycle for one reusable sandbox per visible chat."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        runner: SandboxRunner,
        settings: Settings,
        denials: PermissionDeniedContext,
    ) -> None:
        denials.assert_tenant(tenant_id)
        if not denials.actor.is_system:
            denials.assert_user(tenant_id, owner_id)
        self._denials = denials
        self._session = session
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._settings = settings
        self._chats = ChatSessionRepository(session, tenant_id)
        self._sessions = SandboxSessionRepository(session, tenant_id)
        self._runs = CodeRunRepository(session, tenant_id)
        self._audit_sink = AuditSink(AuditEventRepository(session, tenant_id))
        self._request_id = denials.request_id
        self._source_ip = denials.source_ip

    @audited_resource("sandbox_session.read", "chat_session", "chat_session_id")
    async def get(self, chat_session_id: UUID) -> SandboxSession | None:
        await self._require_visible_chat(chat_session_id, attempted_action="sandbox_session.read")
        return await self._sessions.get_for_chat(chat_session_id)

    async def is_enabled(self) -> bool:
        """Whether deploy + tenant policy currently admit code execution."""
        policy = await SandboxPolicyReader(
            self._session, tenant_id=self._tenant_id, settings=self._settings
        ).resolve()
        return policy.enabled

    async def ensure(self, chat_session_id: UUID) -> SandboxSession:
        await self._require_visible_chat(chat_session_id, attempted_action="sandbox_session.ensure")
        policy = await self._require_enabled(chat_session_id, lifecycle="ensure")
        previous = await self._sessions.get_for_chat(chat_session_id)
        value = await self._sessions.get_or_create(
            owner_id=self._owner_id,
            chat_session_id=chat_session_id,
            image_digest=self._settings.sandbox_image,
        )
        try:
            await self._runner.ensure_session(self._spec(value, policy))
        except Exception:
            await self._sessions.mark_error(value.id, expected_generation=value.generation)
            raise
        if previous is None or previous.status is SandboxSessionStatus.CLOSED:
            await self._audit(
                AuditAction.SANDBOX_SESSION_CREATED,
                value,
                metadata={"generation": value.generation, "image_digest": value.image_digest},
            )
        elif previous.status is SandboxSessionStatus.ERROR:
            await self._audit(
                AuditAction.SANDBOX_SESSION_RESET,
                value,
                metadata={
                    "reason": "error_recovery",
                    "previous_generation": previous.generation,
                    "generation": value.generation,
                },
            )
        return value

    @audited_resource("sandbox_session.reset", "chat_session", "chat_session_id")
    async def reset(self, chat_session_id: UUID) -> SandboxSession:
        await self._require_visible_chat(chat_session_id, attempted_action="sandbox_session.reset")
        policy = await self._require_enabled(chat_session_id, lifecycle="reset")
        current = await self._sessions.get_for_chat(chat_session_id)
        if current is None:
            return await self.ensure(chat_session_id)
        replacement = await self._sessions.advance_generation(
            current.id,
            image_digest=self._settings.sandbox_image,
            expected_generation=current.generation,
        )
        if replacement is None:
            raise ConflictError(
                "Sandbox session changed while it was being reset. Retry the request.",
                code="sandbox_generation_changed",
            )
        try:
            await self._runner.reset_session(
                self._spec(current, policy), self._spec(replacement, policy)
            )
        except Exception:
            await self._sessions.mark_error(current.id, expected_generation=replacement.generation)
            raise
        await self._audit(
            AuditAction.SANDBOX_SESSION_RESET,
            replacement,
            metadata={
                "previous_generation": current.generation,
                "generation": replacement.generation,
            },
        )
        return replacement

    @audited_resource("sandbox_session.close", "chat_session", "chat_session_id")
    async def close(self, chat_session_id: UUID) -> None:
        await self._require_visible_chat(chat_session_id, attempted_action="sandbox_session.close")
        current = await self._sessions.get_for_chat(chat_session_id)
        if current is None or current.status is SandboxSessionStatus.CLOSED:
            return
        await self._runner.close_session(self._spec(current))
        closed = await self._sessions.close(current.id, expected_generation=current.generation)
        if closed is None:
            raise ConflictError(
                "Sandbox session changed while it was being closed. Retry the request.",
                code="sandbox_generation_changed",
            )
        await self._audit(
            AuditAction.SANDBOX_SESSION_CLOSED,
            closed,
            metadata={"generation": closed.generation},
        )

    @audited_resource("code_run.cancel", "code_run", "code_run_id")
    async def cancel(self, code_run_id: UUID) -> CodeRun:
        run = await self._runs.get(code_run_id)
        if run is None or run.owner_id != self._owner_id:
            await self._denials.emit(
                resource_type="code_run",
                resource_id=str(code_run_id),
                attempted_action="code_run.cancel",
                reason="not_visible",
            )
            raise NotFoundError("Code run not found.")
        if run.status not in (CodeRunStatus.QUEUED, CodeRunStatus.RUNNING):
            raise ConflictError(
                "Only a queued or running code run can be cancelled.",
                code="code_run_not_active",
            )
        if run.sandbox_session_id is None or run.sandbox_generation is None:
            # A queued run that has not allocated a container can be terminated
            # without contacting the runner.
            await self._runs.mark_terminal(
                run.id,
                status=CodeRunStatus.KILLED,
                finished_at=datetime.now(UTC),
                stderr="Code execution was cancelled.",
                termination_reason="cancelled",
            )
        else:
            sandbox = await self._sessions.get(run.sandbox_session_id)
            await self._runner.cancel(
                self._build_spec(
                    sandbox_session_id=run.sandbox_session_id,
                    generation=run.sandbox_generation,
                    image=run.image_digest
                    or (
                        sandbox.image_digest
                        if sandbox is not None
                        else self._settings.sandbox_image
                    ),
                ),
                run.id,
            )
            await self._runs.mark_terminal(
                run.id,
                status=CodeRunStatus.KILLED,
                finished_at=datetime.now(UTC),
                stderr="Code execution was cancelled.",
                termination_reason="cancelled",
            )
            replacement = None
            if sandbox is not None and sandbox.generation == run.sandbox_generation:
                replacement = await self._sessions.advance_generation(
                    sandbox.id, expected_generation=sandbox.generation
                )
            if replacement is not None:
                await self._audit(
                    AuditAction.SANDBOX_SESSION_RESET,
                    replacement,
                    metadata={
                        "reason": "cancel",
                        "previous_generation": run.sandbox_generation,
                        "generation": replacement.generation,
                    },
                )
        latest = await self._runs.get(run.id)
        if latest is None:  # pragma: no cover
            raise NotFoundError("Code run not found.")
        await self._audit_sink.emit(
            action=AuditAction.CODE_RUN_CANCELLED,
            actor=AuditActor.user(self._owner_id),
            resource_type="code_run",
            resource_id=str(run.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"sandbox_session_id": str(run.sandbox_session_id)},
        )
        return latest

    async def _require_visible_chat(self, chat_session_id: UUID, *, attempted_action: str) -> None:
        chat = await self._chats.get(chat_session_id)
        if chat is None or chat.owner_id != self._owner_id:
            await self._denials.emit(
                resource_type="chat_session",
                resource_id=str(chat_session_id),
                attempted_action=attempted_action,
                reason="not_visible",
            )
            raise NotFoundError("Chat session not found.")

    async def _require_enabled(
        self, chat_session_id: UUID, *, lifecycle: str
    ) -> EffectiveSandboxPolicy:
        """Refuse a lifecycle action when code execution is off — carrying the reason.

        The error CODE stays ``code_execution_disabled`` (a frozen contract value);
        issue #502 adds the typed ``reason``, which is what an operator surface reads.
        The ``detail`` is the **public** sentence: this error renders into an HTTP
        problem body for any workspace member, so it may not name the deploy
        kill-switch or the process topology (see ``domain/code_execution``).

        The refusal is **audited** (#510). It used to emit only a structlog line, but
        spec 0004 §2.4 puts ops telemetry outside the product audit trail explicitly,
        so a caller hitting a disabled deploy got a 409 that left no trace — while
        every neighbouring sandbox denial audited: the run path writes
        ``code_run.denied`` with its reason code, and the tool runner funnels all four
        refusal paths through ``_finalise``. Lifecycle was the one hole in INV-6.

        ``permission.denied`` rather than a new action: it is already in the MVP
        taxonomy and says exactly what happened — policy refused this action — so the
        gap closes without widening the taxonomy for a single call site.
        """
        policy = await SandboxPolicyReader(
            self._session, tenant_id=self._tenant_id, settings=self._settings
        ).resolve()
        if not policy.enabled:
            reason = policy.disabled_reason or SANDBOX_REASON_TENANT_DISABLED
            log.warning("sandbox.lifecycle_denied", reason_code=reason)
            await self._audit_sink.emit(
                action=AuditAction.PERMISSION_DENIED,
                actor=AuditActor.user(self._owner_id),
                resource_type="sandbox_session",
                # No sandbox session exists to point at — the refusal is the reason
                # there is none — so the chat session it was requested for is the
                # resource an operator would search by.
                resource_id=str(chat_session_id),
                outcome=AuditOutcome.DENIED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={
                    "chat_session_id": str(chat_session_id),
                    "lifecycle": lifecycle,
                    "reason_code": reason,
                },
            )
            raise SandboxDisabledError(reason)
        return policy

    def _spec(
        self, value: SandboxSession, policy: EffectiveSandboxPolicy | None = None
    ) -> SandboxSessionSpec:
        return self._build_spec(
            sandbox_session_id=value.id,
            generation=value.generation,
            image=value.image_digest,
            policy=policy,
        )

    def _build_spec(
        self,
        *,
        sandbox_session_id: UUID,
        generation: int,
        image: str,
        policy: EffectiveSandboxPolicy | None = None,
    ) -> SandboxSessionSpec:
        """The single place a session spec is built, so every path carries one posture.

        With a resolved tenant ``policy`` the bounds are the tenant-narrowed
        effective limits (#589); without one (teardown paths that only need
        the container identity) they are the deployment ceilings.
        """
        if policy is not None:
            limits = resolve_effective_limits(self._settings, policy)
            cpus = limits.cpus
            memory_bytes = limits.memory_bytes
            pids_limit = limits.pids_limit
            wall_clock_seconds = limits.wall_clock_seconds
            scratch_bytes = limits.scratch_bytes
            output_bytes_cap = limits.output_bytes_cap
        else:
            cpus, memory_bytes, pids_limit = session_resource_bounds(self._settings)
            bounded = self._settings.sandbox_session_limits_enabled
            wall_clock_seconds = self._settings.sandbox_wall_clock_seconds
            scratch_bytes = self._settings.sandbox_scratch_bytes if bounded else None
            output_bytes_cap = self._settings.sandbox_output_bytes_cap
        return SandboxSessionSpec(
            sandbox_session_id=sandbox_session_id,
            generation=generation,
            image=image,
            runtime=self._settings.sandbox_runtime,
            # The runner reads collected output files into its own memory and is the
            # single Docker-socket holder, so this bound is what keeps one chat turn's
            # output from taking code execution down for every tenant.
            output_bytes_cap=output_bytes_cap,
            cpus=cpus,
            memory_bytes=memory_bytes,
            pids_limit=pids_limit,
            wall_clock_seconds=wall_clock_seconds,
            scratch_bytes=scratch_bytes,
            env=self._session_env(),
        )

    @staticmethod
    def _session_env() -> tuple[tuple[str, str], ...]:
        return (
            ("HOME", "/root"),
            ("PYTHONUNBUFFERED", "1"),
            ("MPLBACKEND", "Agg"),
            ("LANG", "C.UTF-8"),
        )

    async def _audit(
        self,
        action: AuditAction,
        value: SandboxSession,
        *,
        metadata: dict[str, object],
    ) -> None:
        await self._audit_sink.emit(
            action=action,
            actor=AuditActor.user(self._owner_id),
            resource_type="sandbox_session",
            resource_id=str(value.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"chat_session_id": str(value.chat_session_id), **metadata},
        )


class SandboxService:
    """Execute a code-run row in the chat's reusable sandbox generation."""

    def __init__(
        self,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        runner: SandboxRunner,
        object_store: ObjectStore,
        settings: Settings,
        request_id: str = "sandbox-task",
        source_ip: str = "system",
        accounting: SandboxAccounting | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._store = object_store
        self._settings = settings
        self._request_id = request_id
        self._source_ip = source_ip
        # Admission accounting (#589): Redis-backed and fail-closed by
        # default; tests inject an in-memory implementation.
        self._accounting = accounting or RedisSandboxAccounting(settings.redis_url)
        self._retry_sleep, self._lease_sleep = retry_sleep, lease_sleep

    async def _effective_policy(self, session: AsyncSession) -> EffectiveSandboxPolicy:
        return await SandboxPolicyReader(
            session, tenant_id=self._tenant_id, settings=self._settings
        ).resolve()

    async def execute(
        self,
        session: AsyncSession,
        code_run_id: UUID,
        *,
        inputs: tuple[StagedInput, ...] = (),
    ) -> CodeRunStatus:
        """Execute the run and return only its terminal status.

        The Celery task's contract (``tasks/run_sandbox.py`` returns the status in
        its JSON result). Callers that need to know WHY a run ended where it did —
        the chat seam, which puts it in the ``tool_invocations`` trace — call
        :meth:`execute_outcome` instead.
        """
        return (await self.execute_outcome(session, code_run_id, inputs=inputs)).status

    async def execute_outcome(
        self,
        session: AsyncSession,
        code_run_id: UUID,
        *,
        inputs: tuple[StagedInput, ...] = (),
    ) -> ExecutionOutcome:
        runs = CodeRunRepository(session, self._tenant_id)
        audit = AuditSink(AuditEventRepository(session, self._tenant_id))
        run = await runs.get(code_run_id)
        if run is None:
            return ExecutionOutcome(CodeRunStatus.FAILED)
        if run.status is not CodeRunStatus.QUEUED:
            return ExecutionOutcome(run.status)

        # Every refusal below names the switch that refused (issue #502): the
        # deploy kill-switch, the tenant sandbox policy (absent vs disabled), an
        # unreadable policy, the package policy, or a missing parent chat session.
        # Before this, all of them collapsed into one "disabled for this tenant"
        # line and an operator could not tell which control to change.
        policy = await self._effective_policy(session)
        reason: str | None = None
        packages: tuple[str, ...] = ()
        denial: str | None = None
        # Overrides the reason's stock operator sentence in the audit when the
        # refusal knows something more specific (which package, and why).
        operator_detail: str | None = None
        if not policy.enabled:
            reason = policy.disabled_reason or SANDBOX_REASON_TENANT_DISABLED
            denial = sandbox_reason_public_message(reason)
        elif run.session_id is None:
            reason = SANDBOX_REASON_SESSION_REQUIRED
            denial = sandbox_reason_public_message(reason)
        else:
            try:
                # ``packages`` is what the runner must INSTALL — the requested
                # distributions the execution image does not already ship (#504).
                # Everything the image ships is usable without a fetch, so an empty
                # tenant allow-list no longer refuses `packages=["pandas"]`.
                packages = validate_requested_packages(
                    run.requested_packages,
                    allowed=policy.allowed_packages,
                    denied=policy.denied_packages,
                    preinstalled=self._settings.sandbox_preinstalled_packages,
                )
            except PackagePolicyError as exc:
                reason = SANDBOX_REASON_PACKAGE_DENIED
                # The package refusal names the offending distribution and the
                # allow-list that admits it — tenant product detail, not deployment
                # infrastructure — so it is safe for the model AND is the most
                # useful thing an operator can read (#504).
                denial = exc.detail or sandbox_reason_public_message(reason)
                operator_detail = denial
        limits = resolve_effective_limits(self._settings, policy)
        if denial is not None:
            return await self._finalize_denial(
                runs,
                audit,
                run.id,
                reason=reason or SANDBOX_REASON_RUN_ERROR,
                denial=denial,
                operator_detail=operator_detail,
                limits=limits,
            )

        # Atomic admission (#589): a Redis concurrency lease plus a daily
        # runtime reservation, replacing the racy count-then-act gate. Both
        # fail CLOSED — an accounting store that cannot answer denies the
        # run rather than admitting it unaccounted.
        reserved_seconds = limits.wall_clock_seconds or policy.max_runtime_s
        owner_token = uuid4()
        try:
            leased = await self._accounting.acquire_lease(
                self._tenant_id,
                run.id,
                max_concurrency=limits.max_concurrency,
                ttl_seconds=reserved_seconds + 300,
                owner_token=owner_token,
            )
        except ExecutionAlreadyAdmitted:
            return ExecutionOutcome(CodeRunStatus.RUNNING)
        except AccountingUnavailable:
            return await self._finalize_denial(
                runs, audit, run.id, reason=SANDBOX_REASON_ACCOUNTING_UNAVAILABLE, limits=limits
            )
        if not leased:
            return await self._finalize_denial(
                runs,
                audit,
                run.id,
                reason=SANDBOX_REASON_CONCURRENCY_EXCEEDED,
                operator_detail=(
                    f"This tenant already holds {limits.max_concurrency} sandbox "
                    "concurrency lease(s)."
                ),
                limits=limits,
            )
        reservation_day = datetime.now(UTC)
        started = time.monotonic()
        reserved = False
        lease = AdmissionLease(
            self._accounting,
            self._tenant_id,
            run.id,
            owner_token,
            reserved_seconds + 300,
            self._lease_sleep,
        )
        try:
            try:
                reserved = await self._accounting.reserve_daily(
                    self._tenant_id,
                    seconds=reserved_seconds,
                    daily_cap_s=limits.daily_runtime_cap_s,
                    day=reservation_day,
                )
            except AccountingUnavailable:
                return await self._finalize_denial(
                    runs, audit, run.id, reason=SANDBOX_REASON_ACCOUNTING_UNAVAILABLE, limits=limits
                )
            if not reserved:
                return await self._finalize_denial(
                    runs, audit, run.id, reason=SANDBOX_REASON_DAILY_RUNTIME_EXCEEDED, limits=limits
                )
            return await self._execute_admitted(
                session,
                runs,
                audit,
                run,
                policy=policy,
                limits=limits,
                packages=packages,
                inputs=inputs,
                started_at=reservation_day,
                lease=lease,
            )
        finally:
            await lease.stop()

            # A missed settlement conservatively retains the entire reservation.
            # Never refund into a new day's counter after a midnight boundary.
            async def cleanup() -> None:
                try:
                    if reserved:
                        await self._accounting.settle_daily(
                            self._tenant_id,
                            reserved_seconds=reserved_seconds,
                            actual_seconds=min(
                                reserved_seconds, math.ceil(time.monotonic() - started)
                            ),
                            day=reservation_day,
                        )
                finally:
                    await self._accounting.release_lease(
                        self._tenant_id, run.id, owner_token=owner_token
                    )

            if not lease.pending or lease.confirmed:
                await asyncio.shield(cleanup())

    async def _execute_admitted(
        self,
        session: AsyncSession,
        runs: CodeRunRepository,
        audit: AuditSink,
        run: CodeRun,
        *,
        policy: EffectiveSandboxPolicy,
        limits: EffectiveLimits,
        packages: tuple[str, ...],
        inputs: tuple[StagedInput, ...],
        started_at: datetime,
        lease: AdmissionLease,
    ) -> ExecutionOutcome:
        lifecycle = SandboxSessionService(
            session,
            tenant_id=self._tenant_id,
            owner_id=self._owner_id,
            runner=self._runner,
            settings=self._settings,
            denials=PermissionDeniedContext(
                PermissionDeniedRecorder(
                    db_session.get_durable_audit_transactions(self._settings),
                    tenant_id=self._tenant_id,
                    request_session=session,
                ),
                actor=AuditActor.system(),
                request_id=self._request_id,
                source_ip=self._source_ip,
            ),
        )
        assert run.session_id is not None
        try:
            sandbox = await lifecycle.ensure(run.session_id)
        except SandboxDisabledError as exc:
            # ``ensure`` re-checks enablement, so an admin (or the deploy switch)
            # can turn code execution off between the admission check above and
            # this one. That is still a POLICY refusal and must land exactly where
            # it would have landed a millisecond earlier — a ``denied`` terminal
            # carrying the policy's own reason code — instead of a ``failed`` run
            # blamed on an unclassified crash (issue #502).
            return await self._finalize_denial(
                runs, audit, run.id, reason=exc.sandbox_reason, limits=limits
            )
        except Exception as exc:  # noqa: BLE001 - a run never remains stuck
            log.error(
                "sandbox.session_start_failed",
                code_run_id=str(run.id),
                error_type=type(exc).__name__,
                reason_code=_failure_reason(exc),
            )
            return await self._finalize_failure(
                runs, audit, run.id, started_at, reason=_failure_reason(exc), limits=limits
            )
        session_spec = lifecycle._spec(sandbox, policy)  # noqa: SLF001 - same owning module
        if lease.accounting_lost:
            # Accounting was lost before any remote execution started: refuse
            # fail-closed instead of launching unaccounted work (R3-001).
            return await self._finalize_denial(
                runs, audit, run.id, reason=SANDBOX_REASON_ACCOUNTING_UNAVAILABLE, limits=limits
            )
        running = await runs.mark_running(
            run.id,
            started_at=started_at,
            image_digest=sandbox.image_digest,
            sandbox_session_id=sandbox.id,
            sandbox_generation=sandbox.generation,
            effective_limits=limits.to_dict(),
        )
        if running is None:
            return ExecutionOutcome(CodeRunStatus.FAILED)
        if running.status is not CodeRunStatus.RUNNING:
            return ExecutionOutcome(running.status)
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_STARTED,
            run.id,
            AuditOutcome.ALLOWED,
            {
                "image_digest": sandbox.image_digest,
                "sandbox_session_id": str(sandbox.id),
                "sandbox_generation": sandbox.generation,
                "packages": list(packages),
            },
        )
        # The runner call is bounded by the run's wall clock (#589). Publish the active
        # identity before crossing that boundary so cancel/reset requests can see it,
        # and end this transaction so no pooled DB connection remains checked out for
        # the duration of model-authored code. SET LOCAL is transaction-scoped, so the
        # tenant GUC is rebound before either terminal path touches the database.
        await session.commit()
        spec = RunSpec(
            execution_id=run.id,
            code=run.code,
            packages=packages,
            # The runner re-applies these to the RESOLVED tree (#509): this layer only
            # ever saw the top-level request, and `pip install` takes the whole
            # wheelhouse, so a denied distribution could arrive as a dependency.
            denied_packages=policy.denied_packages,
            wall_clock_seconds=limits.wall_clock_seconds,
            inputs=inputs,
            env=(("LUMEN_OUTPUT_DIR", f"/workspace/.lumen/runs/{run.id}/output"),),
        )
        # The remote call runs as a child task so an accounting-loss heartbeat
        # can terminate only this in-flight call. Cancelling the producer task
        # (user Stop / shutdown) still cancels the child, but a heartbeat tick
        # after the child returns cannot touch local finalisation (R3-001).
        execute_task = asyncio.create_task(self._runner.execute(session_spec, spec))
        lease.attach_in_flight(execute_task)
        try:
            result = await execute_task
        except asyncio.CancelledError:
            producer = asyncio.current_task()
            producer_cancelled = producer is not None and producer.cancelling() > 0
            accounting_loss = lease.accounting_lost and not producer_cancelled
            termination_reason = (
                SANDBOX_REASON_ACCOUNTING_UNAVAILABLE if accounting_loss else "cancelled"
            )
            lease.pending = True
            delay = 1.0
            failure_audited = False
            attempt = 0
            while True:
                attempt += 1
                try:
                    await self._runner.cancel(session_spec, run.id)
                    await bind_tenant(session, self._tenant_id)
                    await runs.mark_terminal(
                        run.id,
                        status=CodeRunStatus.KILLED,
                        finished_at=datetime.now(UTC),
                        stderr=(
                            sandbox_reason_public_message(SANDBOX_REASON_ACCOUNTING_UNAVAILABLE)
                            if accounting_loss
                            else ""
                        ),
                        termination_reason=termination_reason,
                    )
                    if accounting_loss:
                        # Distinct from a user Stop: a system-forced terminal,
                        # not an allowed ``code_run.cancelled`` (#830 composes
                        # user Stop on the cancelled path).
                        await self._audit_run(
                            audit,
                            AuditAction.CODE_RUN_FINISHED,
                            run.id,
                            AuditOutcome.ERROR,
                            {
                                "status": CodeRunStatus.KILLED.value,
                                "termination_reason": termination_reason,
                                "reason_code": SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
                                "effective_limits": limits.to_dict(),
                            },
                        )
                    else:
                        await self._audit_run(
                            audit,
                            AuditAction.CODE_RUN_CANCELLED,
                            run.id,
                            AuditOutcome.ALLOWED,
                            {
                                "termination_reason": "cancelled",
                                "effective_limits": limits.to_dict(),
                            },
                        )
                    await session.commit()
                    lease.confirmed = True
                    break
                except (Exception, asyncio.CancelledError) as exc:
                    reason_code = (
                        "sandbox_cancel_interrupted"
                        if isinstance(exc, asyncio.CancelledError)
                        else _failure_reason(exc)
                    )
                    log.warning(
                        "sandbox.kill_unconfirmed",
                        code_run_id=str(run.id),
                        reason_code=reason_code,
                        attempt=attempt,
                    )
                    # Roll back the failed transaction before auditing. No raw
                    # transport diagnostics enter the append-only product audit.
                    # Audit only the first failure (and the final outcome above)
                    # so an unreachable runner cannot flood the audit trail with
                    # one row per retry; every attempt is still logged (R3-002).
                    await session.rollback()
                    await bind_tenant(session, self._tenant_id)
                    if not failure_audited:
                        await self._audit_run(
                            audit,
                            AuditAction.CODE_RUN_CANCELLED,
                            run.id,
                            AuditOutcome.ERROR,
                            {"reason_code": reason_code},
                        )
                        failure_audited = True
                    await session.commit()
                # Producer shutdown here abandons cleanup without refunding and
                # stops renewal. A kill interrupted above remains retryable.
                # Exponential backoff with a ceiling (R3-002).
                await self._retry_sleep(delay)
                delay = min(delay * 2, 60.0)
            if accounting_loss and not (producer is not None and producer.cancelling() > 0):
                return ExecutionOutcome(CodeRunStatus.KILLED, SANDBOX_REASON_ACCOUNTING_UNAVAILABLE)
            raise
        except Exception as exc:  # noqa: BLE001 - a run never remains stuck
            log.error(
                "sandbox.run_failed",
                code_run_id=str(run.id),
                error_type=type(exc).__name__,
                reason_code=_failure_reason(exc),
            )
            await bind_tenant(session, self._tenant_id)
            return await self._finalize_failure(
                runs, audit, run.id, started_at, reason=_failure_reason(exc), limits=limits
            )
        finally:
            lease.detach_in_flight(execute_task)

        await bind_tenant(session, self._tenant_id)
        artifact_ids = await self._capture_outputs(session, result, run.id, run.session_id)
        terminal = await runs.mark_terminal(
            run.id,
            status=result.status,
            finished_at=datetime.now(UTC),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            resource_usage=result.resource_usage,
            image_digest=result.image_digest or sandbox.image_digest,
            artifact_ids=artifact_ids,
            resolved_packages=result.resolved_packages or None,
            termination_reason=result.termination_reason
            or ("completed" if result.status is CodeRunStatus.SUCCEEDED else result.status.value),
        )
        effective_status = terminal.status if terminal is not None else CodeRunStatus.FAILED
        await SandboxSessionRepository(session, self._tenant_id).touch(sandbox.id)
        limit_reason = (
            terminal.termination_reason if terminal is not None else result.termination_reason
        )
        if not limit_reason or not limit_reason.startswith("sandbox_limit_"):
            limit_reason = None
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_FINISHED,
            run.id,
            AuditOutcome.ERROR if limit_reason else AuditOutcome.ALLOWED,
            {
                "status": effective_status.value,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "artifact_count": len(artifact_ids),
                "termination_reason": terminal.termination_reason
                if terminal
                else result.termination_reason,
                "effective_limits": limits.to_dict(),
                "reason_code": limit_reason,
            },
        )
        return ExecutionOutcome(effective_status, limit_reason)

    async def _capture_outputs(
        self,
        session: AsyncSession,
        result: RunResult,
        code_run_id: UUID,
        chat_session_id: UUID | None,
    ) -> list[UUID]:
        if not result.output_files:
            return []
        artifacts = ArtifactsService(
            session,
            tenant_id=self._tenant_id,
            owner_id=self._owner_id,
            object_store=self._store,
            audit=AuditSink(AuditEventRepository(session, self._tenant_id)),
            denials=None,  # output capture is create-only; no direct-resource guard
            request_id=self._request_id,
            source_ip=self._source_ip,
            artifact_allowed_content_types=self._settings.artifact_allowed_content_types,
            max_artifact_bytes=self._settings.max_artifact_bytes,
            retention_days=self._settings.artifact_retention_days,
        )
        ids: list[UUID] = []
        for output in result.output_files:
            try:
                artifact = await artifacts.create_artifact(
                    data=output.data,
                    filename=output.filename,
                    content_type=output.content_type,
                    produced_by=ArtifactProducedBy.TOOL,
                    links=ArtifactLinks(
                        session_id=chat_session_id,
                        run_id=code_run_id,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - one rejected file does not hide result
                log.warning(
                    "sandbox.artifact_rejected",
                    code_run_id=str(code_run_id),
                    filename=output.filename,
                    error_type=type(exc).__name__,
                )
                continue
            ids.append(artifact.id)
        return ids

    async def _finalize_denial(
        self,
        runs: CodeRunRepository,
        audit: AuditSink,
        code_run_id: UUID,
        *,
        reason: str,
        denial: str | None = None,
        operator_detail: str | None = None,
        limits: EffectiveLimits | None = None,
    ) -> ExecutionOutcome:
        """Write the ``denied`` terminal for a policy refusal, saying WHICH (#502).

        The single writer for both enablement checks, so a policy that flips
        mid-admission produces the same terminal, the same audit action and the
        same ``reason_code`` as one that was already off.

        ``denial`` is the model-facing sentence (defaults to the reason's public
        one); ``operator_detail`` overrides the reason's stock operator sentence
        when the refusal knows something more specific (which package, and why).
        """
        log.warning("sandbox.run_denied", code_run_id=str(code_run_id), reason_code=reason)
        await runs.mark_terminal(
            code_run_id,
            status=CodeRunStatus.DENIED,
            finished_at=datetime.now(UTC),
            # This row's stderr IS the model-facing reply, so it carries the
            # control-neutral sentence only. The operator sentence goes to the log
            # line above and the audit metadata below (issue #502).
            stderr=denial if denial is not None else sandbox_reason_public_message(reason),
            duration_ms=0,
            effective_limits=limits.to_dict() if limits is not None else None,
            termination_reason=reason,
        )
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_DENIED,
            code_run_id,
            AuditOutcome.DENIED,
            {
                "reason": operator_detail or sandbox_reason_message(reason),
                "reason_code": reason,
                "effective_limits": limits.to_dict() if limits else None,
            },
        )
        return ExecutionOutcome(CodeRunStatus.DENIED, reason)

    async def _finalize_failure(
        self,
        runs: CodeRunRepository,
        audit: AuditSink,
        code_run_id: UUID,
        started_at: datetime,
        *,
        reason: str = SANDBOX_REASON_RUN_ERROR,
        limits: EffectiveLimits | None = None,
    ) -> ExecutionOutcome:
        """Terminate a run that could not complete, saying WHAT could not (#502).

        An unreachable ``sandbox-runner`` is a **dependency** fault, not a policy
        denial, so the run stays ``failed`` — but it must say so, or an operator
        whose four policy switches are all green has nothing left to look at. The
        run's ``stderr`` is the model-facing reply, so it gets the control-neutral
        sentence; the operator sentence rides in the audit metadata beside the code.
        """
        finished_at = datetime.now(UTC)
        terminal = await runs.mark_terminal(
            code_run_id,
            status=CodeRunStatus.FAILED,
            finished_at=finished_at,
            stderr=sandbox_reason_public_message(reason),
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            effective_limits=limits.to_dict() if limits is not None else None,
            termination_reason=reason,
        )
        effective_status = terminal.status if terminal is not None else CodeRunStatus.FAILED
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_FINISHED,
            code_run_id,
            AuditOutcome.ERROR,
            {
                "status": effective_status.value,
                "reason_code": reason,
                "effective_limits": limits.to_dict() if limits else None,
                "reason": sandbox_reason_message(reason),
            },
        )
        return ExecutionOutcome(effective_status, reason)

    async def _audit_run(
        self,
        audit: AuditSink,
        action: AuditAction,
        code_run_id: UUID,
        outcome: AuditOutcome,
        metadata: dict[str, object],
    ) -> None:
        await audit.emit(
            action=action,
            actor=AuditActor.user(self._owner_id),
            resource_type="code_run",
            resource_id=str(code_run_id),
            outcome=outcome,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )


class SandboxReadService:
    """Owner/tenant-scoped read service for code-run inspection."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        denials: PermissionDeniedContext,
    ) -> None:
        denials.assert_user(tenant_id, owner_id)
        self._denials = denials
        self._code_runs = CodeRunRepository(session, tenant_id)
        self._owner_id = owner_id

    @audited_resource("code_run.read", "code_run", "code_run_id")
    async def get(self, code_run_id: UUID) -> CodeRun:
        run = await self._code_runs.get(code_run_id)
        if run is None or run.owner_id != self._owner_id:
            await self._denials.emit(
                resource_type="code_run",
                resource_id=str(code_run_id),
                attempted_action="code_run.read",
                reason="not_visible",
            )
            raise NotFoundError("Code run not found.")
        return run


__all__ = [
    "ExecutionOutcome",
    "PackagePolicyError",
    "SandboxReadService",
    "SandboxDisabledError",
    "SandboxService",
    "SandboxSessionService",
    "resolve_effective_limits",
    "session_resource_bounds",
    "validate_requested_packages",
]
