"""Headless agent-run use-cases — enqueue, execute, and read run records (ADR-0015, #235).

The orchestration layer for the ``/runs`` surface **and** the headless execution
path (ADR-0004: ``services/`` compose adapters; routers call one service, the
Celery task calls the execution core). Three concerns live here:

* **Enqueue** (:func:`enqueue_manual_run`) — the ``run-now`` / manual-trigger entry
  point: create a ``queued`` :class:`~app.domain.entities.Run` pinned to the
  assistant's current published version, then enqueue the Celery task. Used later
  by the scheduler (#236) run-now and the assistant test harness (#215).
* **Execute** (:func:`execute_run`) — the headless runtime core the Celery task
  drives under ``tenant_session_scope``: load the run + assistant (+ pinned
  version), inject the assistant config (tool allow-list via the registry,
  knowledge scope), run the **shared chat runtime** against a
  :class:`~app.services.run_sink.RunTranscriptSink` (no socket present), persist the
  transcript + summary + terminal status, and audit ``run.started``/``run.finished``
  (INV-6). The runtime's ``Principal`` is the run **owner**, so ``retrieval/`` admits
  exactly what that user could retrieve interactively — a headless run can never
  read what its runner can't (INV-2, the load-bearing property of the epic). A crash
  writes ``failed`` with a typed error, never a stuck ``running`` (ADR-0015 §5).
* **Read** (:class:`RunsReadService`) — the ``GET /runs`` inbox + ``GET /runs/{id}``
  detail, owner- and tenant-scoped (a cross-tenant / non-owned run id → 404,
  existence non-disclosure, INV-1/INV-2), with the transcript + grounded citations
  hydrated on the detail.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** Every
read is scoped to the caller's tenant (the repository) *and* ownership (this
service). The ``tenant_id``/``owner_id`` come from the resolved principal, never
request input.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    ChatSessionRepository,
    CitationRepository,
    CitationView,
    RunRepository,
    RunStepRepository,
    UserRepository,
)
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AssistantStatus,
    AuditOutcome,
    Run,
    RunError,
    RunStatus,
    RunStep,
    RunTrigger,
)
from app.llm import LLMGateway
from app.services.assistant_runtime import AssistantRunConfig, assemble_run_config
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime
from app.services.mcp_servers_service import build_mcp_servers_service
from app.services.models_service import is_allowed_model
from app.services.run_sink import RunTranscriptSink
from app.services.tools.types import ToolDefinition

log = get_logger(__name__)

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

_RUN_CURSOR_PREFIX = "run:"


# --- Cursor codec (opaque; carries the boundary row id) ---------------------


def _encode_cursor(row_id: UUID) -> str:
    raw = f"{_RUN_CURSOR_PREFIX}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_RUN_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_RUN_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _resolve_model(config_model: str | None, settings: Settings) -> str:
    """The model a headless run uses (fail-closed): the version's frozen model, else default.

    Mirrors the chat path's precedence: an assistant version's frozen ``model`` wins
    when it is still a registry id; otherwise the server default. A model that has
    since left the registry never strands a run (fail-closed on availability, never
    on permission — the retrieval filter still runs as the owner).
    """
    if config_model is not None and is_allowed_model(config_model, settings):
        return config_model
    for m in settings.chat_model_registry:
        if m.is_default:
            return m.id
    return settings.chat_model_registry[0].id  # pragma: no cover — validator guarantees a default


# --- Read views -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunPage:
    """One page of runs (newest first) + the opaque cursor for the next page."""

    items: list[Run]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class RunDetail:
    """A run + its ordered transcript + grounded citations (contract ``Run`` detail)."""

    run: Run
    steps: list[RunStep]
    citations: list[CitationView]


# --- Enqueue (manual trigger / run-now) -------------------------------------


async def enqueue_manual_run(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    owner_id: UUID,
    assistant_id: UUID,
    inputs: dict[str, object] | None = None,
    trigger: RunTrigger = RunTrigger.MANUAL,
    schedule_id: UUID | None = None,
) -> Run:
    """Create a ``queued`` run for an assistant, then enqueue its task (ADR-0015 §4).

    The ``run-now`` entry point (reused by the scheduler #236 run-now + a fire, and
    the test harness #215). Resolves the assistant owner-scoped (cross-tenant /
    non-owned → 404, INV-1/INV-2); requires it **published** with a pinned head
    version (else 422, INV-8 — a run must be reproducible against a frozen version;
    a **disabled** assistant is not ``PUBLISHED`` so it is rejected here too — the
    "a disabled/unauthorized assistant cannot be run" negative). Snapshots the
    ``inputs`` and pins ``assistant_version_id``. ``trigger`` distinguishes a
    ``manual`` run-now from a ``schedule`` fire; ``schedule_id`` links a fired run to
    its schedule. The Celery enqueue is **after-commit** by the caller so the
    request returns immediately and a broker outage never rolls back the queued run.

    Returns the created run; the caller enqueues ``run_assistant(run.id)`` once the
    row is durable (mirroring ``enqueue_ingestion``).
    """
    assistant = await AssistantRepository(session, tenant_id).get(assistant_id)
    if assistant is None or assistant.owner_id != owner_id:
        # Cross-tenant / non-owned → 404 (existence non-disclosure, INV-1/INV-2).
        raise NotFoundError("Assistant not found.")
    if assistant.status is not AssistantStatus.PUBLISHED:
        raise ValidationError(
            "Only a published assistant can be run.", code="assistant_not_published"
        )
    head = await AssistantVersionRepository(session, tenant_id).get_head(assistant.id)
    if head is None:
        raise ValidationError(
            "The assistant has no published version to run.", code="assistant_not_published"
        )
    return await RunRepository(session, tenant_id).create(
        owner_id=owner_id,
        assistant_id=assistant.id,
        assistant_version_id=head.id,
        trigger=trigger,
        inputs=inputs,
        schedule_id=schedule_id,
    )


# --- Execute (the headless runtime core the Celery task drives) -------------


async def execute_run(
    run_id: UUID,
    tenant_id: UUID,
    *,
    settings: Settings | None = None,
    gateway: LLMGateway | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    request_id: str = "run-task",
    source_ip: str = "system",
) -> RunStatus:
    """Execute one headless run end-to-end and persist its terminal outcome (ADR-0015 §4).

    The async core the Celery task drives (a thin sync wrapper). Runs under
    ``tenant_session_scope(tenant_id)`` so the whole run executes **as its tenant**
    with the RLS backstop armed (INV-1). Steps:

    1. Load the run (``queued``) + its assistant version; a run whose assistant was
       deleted (or is not ``queued``) short-circuits to ``failed`` — never a stuck
       ``running``.
    2. Reconstruct the run **owner** as the runtime :class:`Principal` (INV-2 — the
       retrieval filter admits exactly what that user could retrieve), audit
       ``run.started``, and mark the run ``running``.
    3. Create an internal chat session pinned to the assistant version (so the
       transcript's message + citations persist through the **shared** chain, INV-3),
       and drive the shared :class:`ChatRuntime` against a
       :class:`RunTranscriptSink` — no socket present. A live watcher would attach
       over the chat WS with the run id as the ``streamId`` (the run task itself does
       not dual-publish; the scheduler wires the tee when a watcher is present).
    4. Fold the sink's terminal into the run status (``done`` → ``succeeded`` +
       summary; ``error`` → ``failed`` + typed error), persist the transcript, and
       audit ``run.finished`` with the terminal status.

    Any unexpected exception marks the run ``failed`` with a typed, opaque error and
    audits ``run.finished`` — **a run never ends in silence** (ADR-0015 §5). Returns
    the terminal :class:`RunStatus`.
    """
    settings = settings or get_settings()
    gateway = gateway or LLMGateway(settings)
    factory = sessionmaker

    def _sessionmaker() -> async_sessionmaker[AsyncSession]:
        # Lazy so the shared engine (used by ``tenant_session_scope``) is the one the
        # runtime writes through; injectable for the offline tests.
        if factory is not None:
            return factory
        from app.db.session import get_sessionmaker

        return get_sessionmaker()

    # --- Phase 1: claim the run + resolve the principal/config (own txn). ----
    async with tenant_session_scope(tenant_id) as session:
        runs = RunRepository(session, tenant_id)
        run = await runs.get(run_id)
        if run is None:
            # The run was deleted / never existed in this tenant — idempotent no-op.
            return RunStatus.FAILED
        if run.status is not RunStatus.QUEUED:
            # Already claimed (a duplicate delivery) — do not re-run; report its state.
            return run.status

        audit = AuditSink(AuditEventRepository(session, tenant_id))
        principal = await _load_owner_principal(session, tenant_id, run.owner_id)
        config = await _load_run_config(session, tenant_id, run.assistant_version_id)
        if principal is None or config is None:
            # The owner or the pinned version is gone — cannot run reproducibly.
            reason = "owner_missing" if principal is None else "version_missing"
            await _fail_run(
                runs,
                audit,
                run,
                RunError(code=reason, message="The run's owner or pinned version is unavailable."),
            )
            return RunStatus.FAILED

        model = _resolve_model(config.model, settings)
        # Create the internal session the shared runtime persists against (pinned to
        # the version so the transcript is reproducible; owned by the run owner).
        chat_session = await ChatSessionRepository(session, tenant_id).create(
            owner_id=run.owner_id,
            model=model,
            title=f"run:{run.id}",
            assistant_id=run.assistant_id,
            assistant_version_id=run.assistant_version_id,
        )
        session_id = chat_session.id
        started_at = datetime.now(UTC)
        await runs.mark_running(run.id, started_at=started_at)
        await _audit_run(
            audit,
            action=AuditAction.RUN_STARTED,
            run=run,
            request_id=request_id,
            source_ip=source_ip,
            metadata={
                "assistant_id": str(run.assistant_id),
                "trigger": run.trigger.value,
                "session_id": str(session_id),
            },
        )

    # --- Phase 2: run the shared runtime against the transcript sink. --------
    # Outside the claim txn: the runtime owns its own session (bound to the tenant
    # by ``ChatRuntime.run``'s ``bind_tenant``). No socket present — the sink captures
    # the stream. Any failure here is caught below and written as a terminal.
    question = _question_from_inputs(config, run.inputs)
    sink = RunTranscriptSink(stream_id=str(run.id))
    runtime = ChatRuntime(
        sessionmaker=_sessionmaker(),
        gateway=gateway,
        backplane=sink,
        principal=principal,
        request_id=request_id,
        source_ip=source_ip,
        default_max_tool_turns=settings.chat_max_tool_turns,
        mcp_tools_factory=_build_run_mcp_tools_factory(
            principal=principal,
            settings=settings,
            request_id=request_id,
            source_ip=source_ip,
        ),
    )
    try:
        await runtime.run(
            stream_id=str(run.id),
            session_id=session_id,
            question=question,
            model=model,
            history=[],
            collection_ids=None,
            assistant_config=config,
        )
    except Exception as exc:  # noqa: BLE001 — a run never ends in silence
        # The runtime maps its own errors to a terminal ``error`` envelope, but an
        # error escaping it (or a defect here) must still write a terminal run status.
        log.error("runs.execute_failed", run_id=str(run.id), error_type=type(exc).__name__)
        async with tenant_session_scope(tenant_id) as session:
            runs = RunRepository(session, tenant_id)
            audit = AuditSink(AuditEventRepository(session, tenant_id))
            failed = await runs.get(run.id)
            if failed is not None and not failed.is_terminal:
                await _fail_run(
                    runs,
                    audit,
                    failed,
                    RunError(code="internal_error", message="The run failed unexpectedly."),
                    request_id=request_id,
                    source_ip=source_ip,
                )
        return RunStatus.FAILED

    # --- Phase 3: fold the terminal, persist the transcript, audit finish. ---
    outcome = sink.outcome()
    async with tenant_session_scope(tenant_id) as session:
        runs = RunRepository(session, tenant_id)
        steps = RunStepRepository(session, tenant_id)
        audit = AuditSink(AuditEventRepository(session, tenant_id))
        current = await runs.get(run.id)
        if current is None:  # pragma: no cover — the run row cannot vanish mid-flight
            return outcome.status
        await sink.persist(runs=runs, steps=steps, run_id=run.id)
        await runs.mark_terminal(
            run.id,
            status=outcome.status,
            finished_at=datetime.now(UTC),
            summary=outcome.summary,
            message_id=sink.message_id(),
            session_id=session_id,
            error=outcome.error,
        )
        await _audit_run(
            audit,
            action=AuditAction.RUN_FINISHED,
            run=current,
            request_id=request_id,
            source_ip=source_ip,
            metadata={
                "status": outcome.status.value,
                "session_id": str(session_id),
                **({"error_code": outcome.error.code} if outcome.error is not None else {}),
            },
        )
    return outcome.status


async def _fail_run(
    runs: RunRepository,
    audit: AuditSink,
    run: Run,
    error: RunError,
    *,
    request_id: str = "run-task",
    source_ip: str = "system",
) -> None:
    """Write a run's ``failed`` terminal + audit ``run.finished`` (the crash-safe path)."""
    await runs.mark_terminal(
        run.id,
        status=RunStatus.FAILED,
        finished_at=datetime.now(UTC),
        error=error,
    )
    await _audit_run(
        audit,
        action=AuditAction.RUN_FINISHED,
        run=run,
        request_id=request_id,
        source_ip=source_ip,
        metadata={"status": RunStatus.FAILED.value, "error_code": error.code},
    )


async def _audit_run(
    audit: AuditSink,
    *,
    action: AuditAction,
    run: Run,
    request_id: str,
    source_ip: str,
    metadata: dict[str, object],
) -> None:
    """Emit a ``run.started``/``run.finished`` event (INV-6) — actor is the run owner.

    A scheduled/manual run acts **on the owner's behalf**, so the audited actor is
    the owner (not "system"): the trail shows *who* the run ran as, tagged with the
    trigger in metadata.
    """
    await audit.emit(
        action=action,
        actor=AuditActor.user(run.owner_id),
        resource_type="run",
        resource_id=str(run.id),
        outcome=AuditOutcome.ALLOWED,
        request_id=request_id,
        source_ip=source_ip,
        metadata=metadata,
    )


def _build_run_mcp_tools_factory(
    *,
    principal: Principal,
    settings: Settings,
    request_id: str,
    source_ip: str,
) -> Callable[[AsyncSession], Awaitable[dict[str, ToolDefinition]]]:
    """The MCP-tools resolver a scheduled/manual assistant run uses (issue #227).

    A headless run executes **as** the assistant's owner (``principal``), so its MCP
    tools are resolved from exactly that owner's registered+enabled servers in the
    tenant — the same owner-scoped, SSRF-guarded, CC-C-auth path an interactive chat
    uses. The runtime calls this only when the pinned version's allow-list names an
    ``mcp:*`` tool; a native-only assistant never touches the MCP servers table.
    """

    async def _factory(session: AsyncSession) -> dict[str, ToolDefinition]:
        audit = AuditSink(AuditEventRepository(session, principal.tenant_id))
        service = build_mcp_servers_service(
            session,
            settings=settings,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            roles=principal.roles,
            audit=audit,
            request_id=request_id,
            source_ip=source_ip,
        )
        return await service.resolve_run_tools()

    return _factory


async def _load_owner_principal(
    session: AsyncSession, tenant_id: UUID, owner_id: UUID
) -> Principal | None:
    """Rebuild the run owner as the runtime :class:`Principal` (INV-2).

    The runtime executes **as** this principal, so ``retrieval/`` keys its permission
    filter off exactly the owner's identity + roles — a headless run inherits the
    owner's grants, never more. ``None`` if the owner was deprovisioned (the run's
    ``owner_id`` FK is ``SET NULL``-guarded; the run then fails reproducibly).
    """
    user = await UserRepository(session, tenant_id).get(owner_id)
    if user is None:
        return None
    return Principal.from_user(user)


async def _load_run_config(
    session: AsyncSession, tenant_id: UUID, assistant_version_id: UUID | None
) -> AssistantRunConfig | None:
    """Assemble the run config from the pinned assistant version (ADR-0015 §4).

    The version's frozen config → the three run inputs the shared runtime consumes
    (instructions → system prompt, tool_allowlist → the CC-A allowed set,
    knowledge_scope → the narrowing retrieval filter). ``None`` if the pinned version
    is gone (a run must execute against a frozen version to be reproducible).
    """
    if assistant_version_id is None:
        return None
    version = await AssistantVersionRepository(session, tenant_id).get(assistant_version_id)
    if version is None:
        return None
    return assemble_run_config(version.config)


def _question_from_inputs(config: AssistantRunConfig, inputs: dict[str, object]) -> str:
    """The prompt the run answers, from the snapshotted inputs (ADR-0015 §2).

    v1 reads an explicit ``prompt`` / ``question`` input if present; otherwise it
    falls back to a stable directive so a parameter-less schedule still runs its
    assistant's instructions (the persona-augmented system prompt carries the real
    task). Richer input-template rendering is the scheduler's concern (#236); this
    keeps the headless path working with or without inputs.
    """
    for key in ("prompt", "question", "query"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Run this assistant and produce its scheduled report."


# --- Read service (GET /runs list + GET /runs/{id} detail) ------------------


class RunsReadService:
    """List the caller's runs + read one run's detail, owner- and tenant-scoped (#235).

    Constructed per-request with the session + the resolved ``tenant_id`` /
    ``owner_id`` (from the token, never request input). The repository enforces
    tenancy (INV-1); this service enforces the owner rule — a run in another tenant
    or owned by another user is **404** (existence non-disclosure), never 403.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, owner_id: UUID) -> None:
        self._runs = RunRepository(session, tenant_id)
        self._steps = RunStepRepository(session, tenant_id)
        self._citations = CitationRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id

    async def list_(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        assistant_id: UUID | None = None,
        schedule_id: UUID | None = None,
        status: RunStatus | None = None,
    ) -> RunPage:
        """A keyset page of the caller's own runs (newest first), with the frozen filters."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._runs.list_for_owner_page(
            self._owner_id,
            limit=page_size + 1,
            after_id=after_id,
            assistant_id=assistant_id,
            schedule_id=schedule_id,
            status=status,
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return RunPage(items=page, next_cursor=next_cursor)

    async def get(self, run_id: UUID) -> RunDetail:
        """The full run detail (transcript + grounded citations); not visible → 404.

        Owner-scoped (INV-1/INV-2): a cross-tenant / non-owned run id is 404, so the
        surface never reveals that a foreign run exists. The transcript comes from
        ``run_steps`` (the durable envelope stream) and the grounded citations from
        the run's assistant message (the shared citation chain, INV-3).
        """
        run = await self._runs.get(run_id)
        if run is None or run.owner_id != self._owner_id:
            raise NotFoundError("Run not found.")
        steps = await self._steps.list_for_run(run.id)
        citations: list[CitationView] = []
        if run.message_id is not None:
            citations = await self._citations.list_for_message_hydrated(run.message_id)
        return RunDetail(run=run, steps=steps, citations=citations)


__all__ = [
    "RunDetail",
    "RunPage",
    "RunsReadService",
    "enqueue_manual_run",
    "execute_run",
]
