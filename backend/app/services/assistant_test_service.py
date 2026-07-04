"""Read-only preview/test/debug of a DRAFT assistant before publishing (E6-5, #215).

The builder capability behind ``POST /assistants/{id}/test``: run an assistant's
**working (draft) config** against a sample input and hand back a **debug trace**
(prompt, retrieval, tool calls with args/results, outputs, approvals, errors,
timing) — with **NO real side effect**. It is the smallest correct reuse of the
existing agent runtime, not a new engine (mirroring ADR-0015 §2's headless refactor):

* The **shared** :class:`~app.services.chat_runtime.ChatRuntime` drives the agentic
  loop exactly as a real chat/run does — so what the builder previews is what will
  actually happen — but against a :class:`~app.services.run_sink.DebugTraceSink`
  (no socket) instead of the Redis backplane, so every ``delta`` / tool event /
  citation is captured for the debug view rather than streamed to a client.
* **Write-tier tools are forced into simulate/deny mode** (the load-bearing
  guarantee, AC-1/AC-N): the runtime is called with ``simulate_writes=True`` (a T1
  ``write_file`` builds + validates but persists **no** artifact) and the
  code-execution seam is left **unwired** (``run_python`` — T2, admin-gated — has no
  sandbox, so it reports a typed ``ok=False`` instead of launching a container). A
  test run therefore produces no artifact, no code run, and no external effect.
* **Nothing is persisted** — a test run creates no ``Run`` and no durable transcript.
  The runtime writes its internal chat session + assistant message through a session
  this service **rolls back** at the end (a non-committing sessionmaker), so a
  preview leaves the database exactly as it found it (no ``chat_sessions`` /
  ``messages`` / ``citations`` rows). Only the owner-gated ``assistant.tested``
  audit event (INV-6) is committed, on the request session.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** The
draft is loaded scoped to the caller's tenant (the repository) *and* ownership
(this service): a cross-tenant / non-owned assistant id is a **404** (existence
non-disclosure), exactly like the rest of the ``/assistants`` surface. A tenant
admin may also test any assistant in the tenant (the same owner-or-admin rule the
CRUD surface uses).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.repositories import AssistantRepository, ChatSessionRepository
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import Assistant, AuditOutcome, Role
from app.llm import LLMGateway
from app.services.assistant_runtime import AssistantRunConfig, assemble_run_config
from app.services.assistants_service import config_from_assistant
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime
from app.services.models_service import is_allowed_model
from app.services.run_sink import DebugTraceSink

log = get_logger(__name__)

# The stable prompt a parameter-less test run answers when the caller supplies no
# input (mirrors the headless run's fallback): the persona-augmented system prompt
# still carries the real task, so a preview with no input still exercises the config.
_DEFAULT_TEST_INPUT = "Run this assistant against a sample request and produce its output."


@dataclass(frozen=True, slots=True)
class AssistantTestTrace:
    """The debug trace a test run produces — the shape the builder debug view renders.

    Everything the runtime emitted, projected for inspection: the effective system
    ``prompt`` (persona-augmented, so the builder sees exactly what the model was
    given), the sample ``input``, the resolved ``model``, the ``retrieval`` grounding
    (permitted passages only, INV-3), each ``tool_call`` with its args + result, the
    streamed ``outputs``, any ``errors`` (typed problem envelopes — never a raw vendor
    string), whether the run ``succeeded``, and the wall-clock ``duration_ms``. It is
    a pure projection over the captured stream — no I/O.
    """

    prompt: str
    input: str
    model: str
    retrieval: list[dict[str, object]] = field(default_factory=list)
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    outputs: str = ""
    errors: list[dict[str, object]] = field(default_factory=list)
    succeeded: bool = False
    duration_ms: int = 0


def _resolve_model(config_model: str | None, settings: Settings) -> str:
    """The model a test run uses (fail-closed): the draft's model, else the default.

    Mirrors the chat/run precedence so a preview reflects the real run: the draft's
    chosen ``model`` wins when it is still a registry id; otherwise the server
    default. A model that has since left the registry never strands the preview.
    """
    if config_model is not None and is_allowed_model(config_model, settings):
        return config_model
    for m in settings.chat_model_registry:
        if m.is_default:
            return m.id
    return settings.chat_model_registry[0].id  # pragma: no cover — validator guarantees a default


class AssistantTestService:
    """Run a read-only preview/test of a draft assistant and return its debug trace (#215).

    Constructed per-request with the session, the resolved ``tenant_id`` / ``owner_id``
    + the caller's ``roles`` (all from the token, never request input), the shared LLM
    gateway, and the audit sink + correlation context. The ownership rule
    (owner-or-admin) and the no-side-effect guarantee live here; the repository
    enforces tenancy (INV-1).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        gateway: LLMGateway,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
        settings: Settings | None = None,
        runtime_sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session = session
        self._principal = principal
        self._tenant_id = principal.tenant_id
        self._owner_id = principal.user_id
        self._assistants = AssistantRepository(session, self._tenant_id)
        self._is_admin = principal.has_role(Role.ADMIN)
        self._gateway = gateway
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._settings = settings or get_settings()
        # The sessionmaker the runtime writes its (rolled-back) internal transcript
        # through. Injectable for the offline tests; defaults to the shared factory.
        self._runtime_sessionmaker = runtime_sessionmaker

    def _may_manage(self, assistant: Assistant) -> bool:
        """Whether the caller may test ``assistant`` (owner or tenant admin)."""
        return self._is_admin or assistant.owner_id == self._owner_id

    async def run_test(
        self, assistant_id: UUID, *, input_text: str | None = None
    ) -> AssistantTestTrace:
        """Execute a read-only test of a draft assistant; return its debug trace (#215).

        Loads the draft owner-scoped (cross-tenant / non-owned → 404, INV-1/INV-2),
        assembles the run config from the **current head** (the draft — no published
        version required), runs the shared runtime against a
        :class:`DebugTraceSink` with ``simulate_writes=True`` and no code-execution
        seam (so write-tier tools are simulated/denied — NO real side effect), and
        audits ``assistant.tested`` (INV-6). Nothing the runtime persists survives:
        the internal chat session + message are written through a session this method
        rolls back, so the preview is side-effect-free apart from the committed audit.
        """
        assistant = await self._assistants.get(assistant_id)
        if assistant is None or not self._may_manage(assistant):
            # Cross-tenant / non-owned → 404 (existence non-disclosure, INV-1/INV-2).
            raise NotFoundError("Assistant not found.")

        config = assemble_run_config(config_from_assistant(assistant))
        model = _resolve_model(config.model, self._settings)
        question = (input_text or "").strip() or _DEFAULT_TEST_INPUT

        sink = DebugTraceSink(stream_id=f"test:{assistant_id}")
        started = datetime.now(UTC)
        await self._drive_runtime(
            config=config, model=model, question=question, sink=sink
        )
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

        await self._audit_tested(
            assistant_id=assistant_id, model=model, ok=sink.finished_ok()
        )

        return AssistantTestTrace(
            prompt=config.system_prompt,
            input=question,
            model=model,
            retrieval=sink.retrieval(),
            tool_calls=sink.tool_calls(),
            outputs=sink.outputs(),
            errors=sink.errors(),
            succeeded=sink.finished_ok() and not sink.errors(),
            duration_ms=duration_ms,
        )

    async def _drive_runtime(
        self,
        *,
        config: AssistantRunConfig,
        model: str,
        question: str,
        sink: DebugTraceSink,
    ) -> None:
        """Run the shared runtime against ``sink`` in ONE rolled-back transaction (no writes).

        The runtime owns its own session (via the sessionmaker) and commits it — but
        this hands it a **shared, non-committing** session: ``commit`` flushes instead
        of committing, and the session's close (``async with … as session`` exit) is
        neutralised so the runtime cannot end the transaction. The setup (the internal
        chat session the runtime persists the message against) and the runtime's own
        writes therefore share **one live transaction**, which this method rolls back
        at the end. So a test run leaves no ``chat_sessions`` / ``messages`` /
        ``citations`` rows behind (AC-1) while keeping FK integrity during the run.
        """
        base = self._runtime_sessionmaker
        if base is None:
            from app.db.session import get_sessionmaker

            base = get_sessionmaker()

        session = base()
        # Rebind commit → flush (writes stay in the transaction, never durable) and
        # neutralise close so the runtime's ``async with self._sessionmaker()`` block
        # does not end the shared transaction — this method owns the rollback.
        original_close = session.close

        async def _flush_only() -> None:
            await session.flush()

        async def _no_close() -> None:  # pragma: no cover — trivial
            return None

        session.commit = _flush_only  # type: ignore[method-assign]
        session.close = _no_close  # type: ignore[method-assign]

        def _factory() -> AsyncSession:
            return session

        try:
            await bind_tenant(session, self._tenant_id)
            # The internal chat session the shared runtime persists the message
            # against — owned by the caller, not pinned to a version (a draft has
            # none). Lives in the shared transaction, so the runtime's later message
            # insert sees it; both are rolled back below.
            chat_session = await ChatSessionRepository(session, self._tenant_id).create(
                owner_id=self._owner_id,
                model=model,
                title="assistant-test-preview",
            )
            runtime = ChatRuntime(
                sessionmaker=_factory,  # type: ignore[arg-type]
                gateway=self._gateway,
                backplane=sink,
                # The runtime runs **as the caller** (exactly like an interactive
                # chat): retrieval/ keys its permission filter off this principal, so
                # a test run can never preview a passage the caller could not already
                # retrieve (INV-2, the same property the real run has).
                principal=self._principal,
                request_id=self._request_id,
                source_ip=self._source_ip,
                default_max_tool_turns=self._settings.chat_max_tool_turns,
            )
            await runtime.run(
                stream_id=sink.stream_id,
                session_id=chat_session.id,
                question=question,
                model=model,
                history=[],
                collection_ids=None,
                assistant_config=config,
                # The load-bearing guarantee: write-tier tools simulate/deny, so a
                # test run performs NO real write (no artifact / no code run).
                simulate_writes=True,
            )
        finally:
            # Discard everything the run wrote (chat session + message + citations +
            # the runtime's internal audit) — a preview is side-effect-free apart from
            # the ``assistant.tested`` event committed on the request session.
            await session.rollback()
            session.close = original_close  # type: ignore[method-assign]
            await session.close()

    async def _audit_tested(self, *, assistant_id: UUID, model: str, ok: bool) -> None:
        """Emit the owner-gated ``assistant.tested`` event (INV-6) on the request session.

        A test run mutates nothing, but it is still automation-adjacent activity a
        reviewer must be able to see: who previewed which draft, against which model,
        and whether the preview completed. Committed on the request session (the only
        durable write a test run makes).
        """
        await self._audit.emit(
            action=AuditAction.ASSISTANT_TESTED,
            actor=AuditActor.user(self._owner_id),
            resource_type="assistant",
            resource_id=str(assistant_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"model": model, "succeeded": ok, "simulated": True},
        )


__all__ = ["AssistantTestService", "AssistantTestTrace"]
