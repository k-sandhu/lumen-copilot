"""Reusable root-capable sandbox lifecycle and execution service (ADR-0020)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CodeRunRepository,
    SandboxSessionRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    ArtifactProducedBy,
    AuditOutcome,
    CodeRun,
    CodeRunStatus,
    SandboxSession,
    SandboxSessionStatus,
)
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec, StagedInput
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.services.sandbox_policy_service import EffectiveSandboxPolicy, SandboxPolicyReader
from app.storage import ObjectStore

log = get_logger(__name__)


class PackagePolicyError(ValidationError):
    """A requested package is malformed or not admitted by the tenant policy."""

    code = "sandbox_package_denied"


def _policy_names(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        "*" if value.strip() == "*" else canonicalize_name(value.strip())
        for value in values
        if value.strip()
    )


def validate_requested_packages(
    requested: tuple[str, ...], *, allowed: tuple[str, ...], denied: tuple[str, ...]
) -> tuple[str, ...]:
    """Validate PEP-508 package requirements; deny URLs and apply deny-wins policy."""
    allowed_names = _policy_names(allowed)
    denied_names = _policy_names(denied)
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
        if name in denied_names or ("*" not in allowed_names and name not in allowed_names):
            raise PackagePolicyError(f"Package '{name}' is not allowed for this tenant.")
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
        request_id: str = "sandbox-request",
        source_ip: str = "system",
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._settings = settings
        self._chats = ChatSessionRepository(session, tenant_id)
        self._sessions = SandboxSessionRepository(session, tenant_id)
        self._runs = CodeRunRepository(session, tenant_id)
        self._audit_sink = AuditSink(AuditEventRepository(session, tenant_id))
        self._request_id = request_id
        self._source_ip = source_ip

    async def get(self, chat_session_id: UUID) -> SandboxSession | None:
        await self._require_visible_chat(chat_session_id)
        return await self._sessions.get_for_chat(chat_session_id)

    async def is_enabled(self) -> bool:
        """Whether deploy + tenant policy currently admit code execution."""
        policy = await SandboxPolicyReader(
            self._session, tenant_id=self._tenant_id, settings=self._settings
        ).resolve()
        return policy.enabled

    async def ensure(self, chat_session_id: UUID) -> SandboxSession:
        await self._require_visible_chat(chat_session_id)
        await self._require_enabled()
        previous = await self._sessions.get_for_chat(chat_session_id)
        value = await self._sessions.get_or_create(
            owner_id=self._owner_id,
            chat_session_id=chat_session_id,
            image_digest=self._settings.sandbox_image,
        )
        try:
            await self._runner.ensure_session(self._spec(value))
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

    async def reset(self, chat_session_id: UUID) -> SandboxSession:
        await self._require_visible_chat(chat_session_id)
        await self._require_enabled()
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
            await self._runner.reset_session(self._spec(current), self._spec(replacement))
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

    async def close(self, chat_session_id: UUID) -> None:
        await self._require_visible_chat(chat_session_id)
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

    async def cancel(self, code_run_id: UUID) -> CodeRun:
        run = await self._runs.get(code_run_id)
        if run is None or run.owner_id != self._owner_id:
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
            )
        else:
            sandbox = await self._sessions.get(run.sandbox_session_id)
            await self._runner.cancel(
                SandboxSessionSpec(
                    sandbox_session_id=run.sandbox_session_id,
                    generation=run.sandbox_generation,
                    image=run.image_digest
                    or (
                        sandbox.image_digest
                        if sandbox is not None
                        else self._settings.sandbox_image
                    ),
                    runtime=self._settings.sandbox_runtime,
                    env=self._session_env(),
                ),
                run.id,
            )
            await self._runs.mark_terminal(
                run.id,
                status=CodeRunStatus.KILLED,
                finished_at=datetime.now(UTC),
                stderr="Code execution was cancelled.",
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

    async def _require_visible_chat(self, chat_session_id: UUID) -> None:
        chat = await self._chats.get(chat_session_id)
        if chat is None or chat.owner_id != self._owner_id:
            raise NotFoundError("Chat session not found.")

    async def _require_enabled(self) -> None:
        policy = await SandboxPolicyReader(
            self._session, tenant_id=self._tenant_id, settings=self._settings
        ).resolve()
        if not policy.enabled:
            raise ConflictError(
                "Code execution is disabled for this tenant.",
                code="code_execution_disabled",
            )

    def _spec(self, value: SandboxSession) -> SandboxSessionSpec:
        return SandboxSessionSpec(
            sandbox_session_id=value.id,
            generation=value.generation,
            image=value.image_digest,
            runtime=self._settings.sandbox_runtime,
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
    ) -> None:
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._store = object_store
        self._settings = settings
        self._request_id = request_id
        self._source_ip = source_ip

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
        runs = CodeRunRepository(session, self._tenant_id)
        audit = AuditSink(AuditEventRepository(session, self._tenant_id))
        run = await runs.get(code_run_id)
        if run is None:
            return CodeRunStatus.FAILED
        if run.status is not CodeRunStatus.QUEUED:
            return run.status

        policy = await self._effective_policy(session)
        denial: str | None = None
        packages: tuple[str, ...] = ()
        if not policy.enabled:
            denial = "Code execution is disabled for this tenant."
        elif run.session_id is None:
            denial = "Reusable code execution requires a parent chat session."
        else:
            try:
                packages = validate_requested_packages(
                    run.requested_packages,
                    allowed=policy.allowed_packages,
                    denied=policy.denied_packages,
                )
            except PackagePolicyError as exc:
                denial = exc.detail or "A requested package is not allowed."
        if denial is not None:
            await runs.mark_terminal(
                run.id,
                status=CodeRunStatus.DENIED,
                finished_at=datetime.now(UTC),
                stderr=denial,
                duration_ms=0,
            )
            await self._audit_run(
                audit,
                AuditAction.CODE_RUN_DENIED,
                run.id,
                AuditOutcome.DENIED,
                {"reason": denial},
            )
            return CodeRunStatus.DENIED

        lifecycle = SandboxSessionService(
            session,
            tenant_id=self._tenant_id,
            owner_id=self._owner_id,
            runner=self._runner,
            settings=self._settings,
            request_id=self._request_id,
            source_ip=self._source_ip,
        )
        assert run.session_id is not None
        started_at = datetime.now(UTC)
        try:
            sandbox = await lifecycle.ensure(run.session_id)
        except Exception as exc:  # noqa: BLE001 - a run never remains stuck
            log.error(
                "sandbox.session_start_failed",
                code_run_id=str(run.id),
                error_type=type(exc).__name__,
            )
            return await self._finalize_failure(runs, audit, run.id, started_at)
        session_spec = lifecycle._spec(sandbox)  # noqa: SLF001 - same owning module
        running = await runs.mark_running(
            run.id,
            started_at=started_at,
            image_digest=sandbox.image_digest,
            sandbox_session_id=sandbox.id,
            sandbox_generation=sandbox.generation,
        )
        if running is None:
            return CodeRunStatus.FAILED
        if running.status is not CodeRunStatus.RUNNING:
            return running.status
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
        # The runner call is intentionally unbounded (ADR-0020). Publish the active
        # identity before crossing that boundary so cancel/reset requests can see it,
        # and end this transaction so no pooled DB connection remains checked out for
        # the duration of model-authored code. SET LOCAL is transaction-scoped, so the
        # tenant GUC is rebound before either terminal path touches the database.
        await session.commit()
        spec = RunSpec(
            execution_id=run.id,
            code=run.code,
            packages=packages,
            inputs=inputs,
            env=(("LUMEN_OUTPUT_DIR", f"/workspace/.lumen/runs/{run.id}/output"),),
        )
        try:
            result = await self._runner.execute(session_spec, spec)
        except Exception as exc:  # noqa: BLE001 - a run never remains stuck
            log.error("sandbox.run_failed", code_run_id=str(run.id), error_type=type(exc).__name__)
            await bind_tenant(session, self._tenant_id)
            return await self._finalize_failure(runs, audit, run.id, started_at)

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
        )
        effective_status = terminal.status if terminal is not None else CodeRunStatus.FAILED
        await SandboxSessionRepository(session, self._tenant_id).touch(sandbox.id)
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_FINISHED,
            run.id,
            AuditOutcome.ALLOWED,
            {
                "status": effective_status.value,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "artifact_count": len(artifact_ids),
            },
        )
        return effective_status

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

    async def _finalize_failure(
        self,
        runs: CodeRunRepository,
        audit: AuditSink,
        code_run_id: UUID,
        started_at: datetime,
    ) -> CodeRunStatus:
        finished_at = datetime.now(UTC)
        terminal = await runs.mark_terminal(
            code_run_id,
            status=CodeRunStatus.FAILED,
            finished_at=finished_at,
            stderr="The sandbox run failed unexpectedly.",
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
        )
        effective_status = terminal.status if terminal is not None else CodeRunStatus.FAILED
        await self._audit_run(
            audit,
            AuditAction.CODE_RUN_FINISHED,
            code_run_id,
            AuditOutcome.ERROR,
            {"status": effective_status.value},
        )
        return effective_status

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

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, owner_id: UUID) -> None:
        self._code_runs = CodeRunRepository(session, tenant_id)
        self._owner_id = owner_id

    async def get(self, code_run_id: UUID) -> CodeRun:
        run = await self._code_runs.get(code_run_id)
        if run is None or run.owner_id != self._owner_id:
            raise NotFoundError("Code run not found.")
        return run


__all__ = [
    "PackagePolicyError",
    "SandboxReadService",
    "SandboxService",
    "SandboxSessionService",
    "validate_requested_packages",
]
