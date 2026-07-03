"""Sandbox execution use-cases — the orchestration for one code run (ADR-0013, #230).

The owning application module for **sandboxed code execution** (ADR-0013 boundary
row): it composes the out-of-process runner (:class:`~app.sandbox.runner.SandboxRunner`),
the tenant-scoped ``code_runs`` repository, the artifact store (CC-B #208, output
capture), the one audit sink (INV-6), and the per-tenant quota gate — exposing
**domain types only** (never the runner's or Docker's wire objects, ADR-0004). **No
other module orchestrates code execution or talks to the runner.**

Two concerns live here:

* **Execute** (:meth:`SandboxService.execute`) — the async core the Celery task
  drives under ``tenant_session_scope``. It walks the ``code_runs`` row through
  ``queued`` → (policy/quota gate) → ``running`` → a terminal, persisting each
  transition and the final captured result; a crash writes ``failed`` (never a stuck
  ``running``, ADR-0013 §5). Output files are collected → artifacts (CC-B). Every run
  is bracketed by ``code_run.started``/``code_run.finished`` and a refusal is
  ``code_run.denied`` (INV-6).
* **Read** (:class:`SandboxReadService`) — the frozen ``GET /code-runs/{id}``: one
  run, owner- and tenant-scoped (a cross-tenant / non-owned id → 404, existence
  non-disclosure, INV-1/INV-2).

**Isolation (ADR-0013 §5, G1–G8).** The service never launches a container itself
(that is the ``sandbox-runner``'s job, §1); it builds the deny-by-default
:class:`~app.sandbox.spec.RunSpec` (network none, no host mounts, minimal curated env
— **no secrets/DB creds**, G5) and hands it to the runner. The enforcement flags are
built in :func:`~app.sandbox.runner.build_container_flags` and asserted by the offline
isolation tests; the true kernel-level behaviour is proven live against the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    CodeRunRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    ArtifactProducedBy,
    AuditOutcome,
    CodeRun,
    CodeRunStatus,
)
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import (
    EgressPolicy,
    RunLimits,
    RunResult,
    RunSpec,
    SandboxPolicy,
    StagedInput,
)
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.storage import ObjectStore

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Denial:
    """A pre-execution refusal (ADR-0013 §6) — the reason a run never launched."""

    code: str
    message: str


def _limits_from_settings(settings: Settings) -> RunLimits:
    """The per-run resource caps from config (G6/G7) — never a literal at the call site."""
    return RunLimits(
        cpus=settings.sandbox_cpus,
        memory_bytes=settings.sandbox_memory_bytes,
        pids=settings.sandbox_pids_limit,
        wall_clock_seconds=settings.sandbox_wall_clock_seconds,
        output_bytes_cap=settings.sandbox_output_bytes_cap,
    )


class SandboxService:
    """Execute one sandbox code run end-to-end, crash-safe and audited (ADR-0013 §4/§5).

    Constructed per-run with the tenant/owner (from the resolved principal, never
    request input), the runner, the object store, config, and the correlation
    context. All policy/quota enforcement and result mapping live here; the runner
    only runs the container it is told to. Deny-by-default: with code execution
    disabled (system flag or, later, per-tenant #233) or a quota exceeded, the run is
    refused **before** the runner is ever called (``status=denied``, audited).
    """

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
    ) -> None:
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._store = object_store
        self._settings = settings
        self._request_id = request_id
        self._source_ip = source_ip

    # --- Policy / quota gate (deny-by-default, ADR-0013 §6) ------------------

    async def _evaluate_gate(self, code_runs: CodeRunRepository) -> _Denial | None:
        """Refuse a run before execution when disabled or over quota (ADR-0013 §6).

        Returns a :class:`_Denial` (→ ``status=denied``, audited) when:

        * the system kill-switch (``SANDBOX_ENABLED``) is off — the sandbox never
          launches for any tenant (the per-tenant admin enable #233 layers on top);
        * the per-tenant **concurrency** cap is already met (too many active runs);
        * the per-tenant **daily-runtime** cap is already exhausted for the window.

        ``None`` ⇒ the run may proceed. Fail-closed: any doubt refuses.
        """
        if not self._settings.sandbox_enabled:
            return _Denial(
                "code_execution_disabled",
                "Code execution is disabled for this tenant.",
            )
        active = await code_runs.count_active()
        # The just-created queued row is included in the count, so the cap is met when
        # the active count exceeds the configured maximum.
        if active > self._settings.sandbox_max_concurrent_per_tenant:
            return _Denial(
                "concurrency_quota_exceeded",
                "The tenant's concurrent code-run limit is reached.",
            )
        window_start = datetime.now(UTC) - timedelta(days=1)
        used_ms = await code_runs.runtime_ms_since(window_start)
        cap_ms = self._settings.sandbox_daily_runtime_seconds_per_tenant * 1000
        if used_ms >= cap_ms:
            return _Denial(
                "daily_runtime_quota_exceeded",
                "The tenant's daily code-run runtime limit is reached.",
            )
        return None

    def _build_spec(self, code: str, inputs: tuple[StagedInput, ...]) -> RunSpec:
        """Build the deny-by-default run spec (ADR-0013 §2/§3/§5).

        Egress is denied (G2–G4), package install is off (ADR-0013 §3), the runtime is
        the configured OCI runtime, and the env is a **minimal curated set** — never
        app secrets, the DB URL, or object-store creds (G5). Inputs are staged
        read-only.
        """
        policy = SandboxPolicy(
            egress=EgressPolicy(allowed=False),  # deny-by-default (G2–G4)
            allow_package_install=False,  # curated pinned image only (ADR-0013 §3)
            runtime=self._settings.sandbox_runtime,
        )
        # G5: the ONLY env the run sees. Deliberately no secret/DB/host env — a
        # headless, non-interactive Python process needs none of it.
        curated_env: tuple[tuple[str, str], ...] = (
            ("HOME", "/sandbox"),
            ("PYTHONUNBUFFERED", "1"),
            ("MPLBACKEND", "Agg"),  # headless matplotlib (ADR-0013 §3)
            ("LANG", "C.UTF-8"),
        )
        return RunSpec(
            code=code,
            limits=_limits_from_settings(self._settings),
            policy=policy,
            inputs=inputs,
            env=curated_env,
        )

    # --- Execute (the Celery-driven core) -----------------------------------

    async def execute(
        self,
        session: AsyncSession,
        code_run_id: UUID,
        *,
        inputs: tuple[StagedInput, ...] = (),
    ) -> CodeRunStatus:
        """Run one ``code_runs`` row to a terminal status, capturing its result.

        The async core the Celery task drives under ``tenant_session_scope`` (INV-1).
        Steps (ADR-0013 §4/§5):

        1. Load the ``queued`` run; a run that vanished / is not ``queued`` is a
           no-op (idempotent — a duplicate delivery never re-runs).
        2. **Gate** (§6): disabled tenant / over quota → mark ``denied``, audit
           ``code_run.denied``, and never touch the runner.
        3. Mark ``running``, audit ``code_run.started``, then call the runner with the
           deny-by-default spec (built here, enforced in the runner flags).
        4. Persist the captured terminal (``succeeded``/``failed``/``timeout``/
           ``killed``) with stdout/stderr/exit/duration/resource-usage; collect any
           output files → artifacts (CC-B, tenant-scoped); audit ``code_run.finished``.

        Any unexpected exception is caught and written as ``failed`` — a run **never
        ends in silence** (INV-8, never a stuck ``running``). Returns the terminal.
        """
        code_runs = CodeRunRepository(session, self._tenant_id)
        audit = AuditSink(AuditEventRepository(session, self._tenant_id))

        run = await code_runs.get(code_run_id)
        if run is None:
            # The run row was deleted / never existed in this tenant — idempotent no-op.
            return CodeRunStatus.FAILED
        if run.status is not CodeRunStatus.QUEUED:
            # Already claimed (a duplicate delivery) — report its state, do not re-run.
            return run.status

        # --- Gate: refuse before execution (deny-by-default). ----------------
        denial = await self._evaluate_gate(code_runs)
        if denial is not None:
            now = datetime.now(UTC)
            await code_runs.mark_terminal(
                code_run_id,
                status=CodeRunStatus.DENIED,
                finished_at=now,
                stderr=denial.message,
                duration_ms=0,
            )
            await self._audit(
                audit,
                action=AuditAction.CODE_RUN_DENIED,
                code_run_id=code_run_id,
                outcome=AuditOutcome.DENIED,
                metadata={"reason": denial.code},
            )
            return CodeRunStatus.DENIED

        # --- Mark running + audit started, then call the runner. -------------
        started_at = datetime.now(UTC)
        await code_runs.mark_running(
            code_run_id, started_at=started_at, image_digest=self._settings.sandbox_image
        )
        await self._audit(
            audit,
            action=AuditAction.CODE_RUN_STARTED,
            code_run_id=code_run_id,
            outcome=AuditOutcome.ALLOWED,
            metadata={"image_digest": self._settings.sandbox_image},
        )

        spec = self._build_spec(run.code, inputs)
        try:
            result = await self._runner.run(
                spec, tmpfs_scratch_bytes=self._settings.sandbox_scratch_bytes
            )
        except Exception as exc:  # noqa: BLE001 — a run never ends in silence
            log.error(
                "sandbox.run_failed",
                code_run_id=str(code_run_id),
                error_type=type(exc).__name__,
            )
            return await self._finalize_failure(
                session, code_runs, audit, code_run_id, started_at
            )

        # --- Capture output files → artifacts (CC-B), then persist terminal. -
        artifact_ids = await self._capture_outputs(session, result, code_run_id, run.session_id)
        finished_at = datetime.now(UTC)
        await code_runs.mark_terminal(
            code_run_id,
            status=result.status,
            finished_at=finished_at,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            resource_usage=result.resource_usage,
            image_digest=result.image_digest or self._settings.sandbox_image,
            artifact_ids=artifact_ids,
        )
        await self._audit(
            audit,
            action=AuditAction.CODE_RUN_FINISHED,
            code_run_id=code_run_id,
            outcome=AuditOutcome.ALLOWED,
            metadata={
                "status": result.status.value,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "artifact_count": len(artifact_ids),
            },
        )
        return result.status

    async def _capture_outputs(
        self,
        session: AsyncSession,
        result: RunResult,
        code_run_id: UUID,
        session_id: UUID | None,
    ) -> list[UUID]:
        """Persist the run's output files as tenant-scoped artifacts (CC-B #208).

        Each collected file becomes an ``Artifact`` (``produced_by=tool``, linked to
        the run) via the same validated, audited path the file-writing tool uses. A
        file that fails the artifact allowlist/cap is skipped (logged) rather than
        failing the whole run — the run's stdout/stderr/exit are still recorded. The
        ids are stored on the ``code_runs`` row (``artifact_ids``).
        """
        if not result.output_files:
            return []
        artifacts = ArtifactsService(
            session,
            tenant_id=self._tenant_id,
            owner_id=self._owner_id,
            object_store=self._store,
            audit=AuditSink(AuditEventRepository(session, self._tenant_id)),
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
                    links=ArtifactLinks(session_id=session_id, run_id=code_run_id),
                )
            except Exception as exc:  # noqa: BLE001 — one bad file must not fail the run
                log.warning(
                    "sandbox.artifact_rejected",
                    code_run_id=str(code_run_id),
                    filename=output.filename,
                    error_type=type(exc).__name__,
                )
                continue
            ids.append(artifact.id)
        return ids

    async def _finalize_failure(
        self,
        session: AsyncSession,
        code_runs: CodeRunRepository,
        audit: AuditSink,
        code_run_id: UUID,
        started_at: datetime,
    ) -> CodeRunStatus:
        """Write a run's ``failed`` terminal + audit ``code_run.finished`` (crash-safe)."""
        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        await code_runs.mark_terminal(
            code_run_id,
            status=CodeRunStatus.FAILED,
            finished_at=finished_at,
            stderr="The sandbox run failed unexpectedly.",
            duration_ms=duration_ms,
        )
        await self._audit(
            audit,
            action=AuditAction.CODE_RUN_FINISHED,
            code_run_id=code_run_id,
            outcome=AuditOutcome.ERROR,
            metadata={"status": CodeRunStatus.FAILED.value},
        )
        return CodeRunStatus.FAILED

    async def _audit(
        self,
        audit: AuditSink,
        *,
        action: AuditAction,
        code_run_id: UUID,
        outcome: AuditOutcome,
        metadata: dict[str, object],
    ) -> None:
        """Emit a ``code_run.*`` event (INV-6) — actor is the run owner.

        The sandbox runs code **on the owner's behalf**, so the audited actor is the
        owner (not "system"): the trail shows *who* ran adversarial-by-assumption
        Python and how it terminated.
        """
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
    """Read one code run, owner- and tenant-scoped (the frozen ``GET /code-runs/{id}``).

    Constructed per-request with the session + the resolved ``tenant_id`` /
    ``owner_id`` (from the token, never request input). The repository enforces
    tenancy (INV-1); this service enforces the owner rule — a run in another tenant or
    owned by another user is **404** (existence non-disclosure), never 403.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, owner_id: UUID) -> None:
        self._code_runs = CodeRunRepository(session, tenant_id)
        self._owner_id = owner_id

    async def get(self, code_run_id: UUID) -> CodeRun:
        """One code run's detail; not visible (cross-tenant / non-owned) → 404 (INV-1/INV-2)."""
        run = await self._code_runs.get(code_run_id)
        if run is None or run.owner_id != self._owner_id:
            raise NotFoundError("Code run not found.")
        return run


__all__ = [
    "SandboxReadService",
    "SandboxService",
]
