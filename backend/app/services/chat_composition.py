"""The ONE ``ChatRuntime`` composition (issue #576).

Interactive chat, Assistant Test, and headless runs used to construct
:class:`~app.services.chat_runtime.ChatRuntime` in three places, each
wiring a different subset of adapters — and none wired an artifact
writer, so a real chat's ``write_file`` could never persist. This module
is the single builder: every mode gets the same model-route resolver,
fallback validator, and context config from ``Settings``; the deliberate
per-mode differences are spelled out in one place:

| Seam | interactive | test | headless |
|---|---|---|---|
| MCP tools (one factory) | yes | no | yes |
| Sandbox (``run_python``) | yes | no (by design) | yes (new, #576/#578) |
| Artifact writer (``write_file``) | yes | no (simulates) | yes |
| Interactive affordances (suggestions, deadlines) | yes | no | no |
| Tool concurrency | configured | **1** (shared rollback session) | configured |

Behaviour-preserving except the deliberate additions: the artifact
writer on interactive + headless, and the sandbox on headless runs
(executing as the run owner, still gated by the deploy/tenant sandbox
policy inside the sandbox service). Per-mode deadlines/budgets are
unchanged — they are the cancel lane's scope; the
:class:`ChatCompositionOptions` bag keeps the signature easy to extend
without touching the three call sites again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import ArtifactRepository, AuditEventRepository
from app.db.tenant_context import bind_tenant
from app.domain.entities import Artifact, ArtifactProducedBy
from app.llm import LLMGateway
from app.llm.context import ContextConfig
from app.realtime.backplane import Backplane
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.tool_runner import ChatSandboxToolRunner
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.services.chat_runtime import (
    ArtifactWriterContext,
    ArtifactWriterFactory,
    ChatRuntime,
    SandboxContext,
)
from app.services.mcp_servers_service import build_mcp_servers_service
from app.services.models_service import ChatModelService
from app.services.provider_models import build_model_route_resolver
from app.services.tools.types import ArtifactWriter, SandboxToolRunner, ToolDefinition
from app.storage import ObjectStore
from app.storage.keys import build_artifact_key

log = get_logger(__name__)

#: The three answer surfaces that share the runtime.
ChatCompositionMode = Literal["interactive", "test", "headless"]

#: Builds the object-store adapter an artifact/sandbox write goes through.
#: Injectable so offline tests substitute a fake store; production builds
#: the real object-storage adapter from ``Settings``.
ObjectStoreFactory = Callable[[], ObjectStore]


@dataclass(frozen=True, slots=True)
class ChatCompositionOptions:
    """The small extension bag for :func:`build_chat_runtime` (#576).

    ``run_id`` is the headless run a produced artifact links back to.
    ``runtime_factory`` preserves the longstanding test seam: callers pass
    their module-level ``ChatRuntime`` name, so existing tests that
    monkeypatch that name keep intercepting construction.
    ``extra_runtime_kwargs`` is the forward-compat slot (e.g. a
    ``retrieval_factory`` override in tests, budget knobs later) merged
    over the composed kwargs.
    """

    run_id: UUID | None = None
    object_store_factory: ObjectStoreFactory | None = None
    runtime_factory: Callable[..., ChatRuntime] | None = None
    extra_runtime_kwargs: dict[str, Any] = field(default_factory=dict)


class BoundArtifactWriter:
    """An :class:`ArtifactWriter` with its origin bound at construction (#576).

    Wraps the tenant/owner-scoped :class:`ArtifactsService` (tenant and
    owner come from the principal there, never tool input) and forces the
    producing-context links — the chat ``session_id`` and, for a headless
    run, the ``run_id`` — so a tool can never redirect its artifact to
    another session or run. The service owns validation and artifact audit;
    this writer coordinates the savepoint and post-session compensation.
    """

    def __init__(
        self,
        service: ArtifactsService,
        *,
        context: ArtifactWriterContext,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        principal: Principal,
        session_id: UUID,
        run_id: UUID | None = None,
    ) -> None:
        self._service = service
        self._context = context
        self._sessionmaker = sessionmaker
        self._store = store
        self._principal = principal
        self._session_id = session_id
        self._run_id = run_id
        self._pending_keys: set[str] = set()
        context.resources.push_async_callback(self._compensate)

    async def _compensate(self) -> None:
        """Resolve uncertain commits by read-back after the answer session closes.

        Each key belongs to this write alone. A committed row keeps its bytes;
        rollback (including a lost upload acknowledgement) removes the object.
        If commit outcome cannot be read, retain bytes and log for reconciliation.
        """
        if not self._pending_keys:
            return
        try:
            async with self._sessionmaker() as session:
                await bind_tenant(session, self._principal.tenant_id)
                repository = ArtifactRepository(session, self._principal.tenant_id)
                for key in self._pending_keys:
                    if not await repository.exists_with_storage_key(key):
                        await self._store.delete_artifact(str(self._principal.tenant_id), key)
        except Exception as exc:
            log.error("artifact.compensation_failed", error_type=type(exc).__name__)

    async def create_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        produced_by: ArtifactProducedBy,
        links: ArtifactLinks | None = None,
    ) -> Artifact:
        object_id = uuid4().hex
        # Reserve the key BEFORE upload: a storage write may succeed and then
        # lose its acknowledgement. Compensation still knows exactly what to remove.
        self._pending_keys.add(
            build_artifact_key(str(self._principal.tenant_id), data, filename, object_id=object_id)
        )
        # A handler failure is folded into a tool result by the runner. Isolate
        # registration + audit so a partial flush cannot leak into a successful answer.
        async with self._context.session.begin_nested():
            return await self._service.create_artifact(
                data=data,
                filename=filename,
                content_type=content_type,
                produced_by=ArtifactProducedBy.TOOL,
                object_id=object_id,
                links=ArtifactLinks(
                    session_id=self._session_id,
                    run_id=self._run_id,
                    message_id=self._context.message_id,
                    tool_invocation_id=links.tool_invocation_id if links is not None else None,
                ),
            )


def build_mcp_tools_factory(
    *,
    principal: Principal,
    settings: Settings,
    request_id: str,
    source_ip: str,
) -> Callable[[AsyncSession], Awaitable[dict[str, ToolDefinition]]]:
    """The ONE live MCP-tools resolver (issue #227; unified in #576).

    Returns a coroutine the runtime invokes **only** when a run's
    allow-list names at least one ``mcp:*`` tool. It builds the
    per-tenant MCP service over the runtime's own session — scoped to the
    running principal (tenant + owner + roles, never model input; for a
    headless run that principal is the run owner) — and resolves its
    registered+enabled MCP tools into governed tool definitions. The
    SSRF guard, per-tenant rate limit, and vault auth resolution live
    inside that service's client.
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
            denials=None,  # run-tool resolution is list-only, with no direct-id guard
            request_id=request_id,
            source_ip=source_ip,
        )
        return await service.resolve_run_tools()

    return _factory


def build_sandbox_factory(
    *,
    principal: Principal,
    backplane: Backplane,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    request_id: str,
    source_ip: str,
    object_store_factory: ObjectStoreFactory | None = None,
) -> Callable[[SandboxContext], SandboxToolRunner | None]:
    """Build the live ``run_python`` seam factory (issue #231).

    Moved here from the chat router in #576 and now shared with headless
    runs, which execute as the run owner. Returns a closure the runtime
    calls **only** when a run's allow-list offers ``run_python``; it
    composes the HTTP sandbox runner + object store into a
    :class:`ChatSandboxToolRunner` scoped to the principal (tenant +
    owner, never model input). The deny-by-default enable/package gate
    lives inside the sandbox service, so this factory only wires; a
    disabled tenant's run is refused downstream as a typed result.
    """
    make_store = object_store_factory or (lambda: ObjectStore(settings))

    def _factory(ctx: SandboxContext) -> SandboxToolRunner | None:
        return ChatSandboxToolRunner(
            sessionmaker=sessionmaker,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            runner=HttpSandboxRunner(
                settings.sandbox_runner_url, token=settings.sandbox_runner_token
            ),
            object_store=make_store(),
            settings=settings,
            backplane=backplane,
            stream_id=ctx.stream_id,
            request_id=request_id,
            source_ip=source_ip,
            next_seq=ctx.next_seq,
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        )

    return _factory


def build_artifact_writer_factory(
    *,
    principal: Principal,
    settings: Settings,
    request_id: str,
    source_ip: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: UUID | None = None,
    object_store_factory: ObjectStoreFactory | None = None,
) -> ArtifactWriterFactory:
    """Build the live ``write_file`` artifact-writer factory (#576).

    The runtime calls the returned factory **only** when a run's
    executable set offers ``write_file`` and writes are not simulated.
    Each writer is an :class:`ArtifactsService` scoped to the principal's
    tenant + owner over the runtime's own session (so the artifact row
    and its audit commit with the answer), wrapped in a
    :class:`BoundArtifactWriter` that pins the session/run origin.
    """
    make_store = object_store_factory or (lambda: ObjectStore(settings))

    def _factory(ctx: ArtifactWriterContext) -> ArtifactWriter | None:
        store = make_store()
        service = ArtifactsService(
            ctx.session,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            object_store=store,
            audit=AuditSink(AuditEventRepository(ctx.session, principal.tenant_id)),
            denials=None,  # create-only seam; no direct-resource guard (sandbox parity)
            request_id=request_id,
            source_ip=source_ip,
            artifact_allowed_content_types=settings.artifact_allowed_content_types,
            max_artifact_bytes=settings.max_artifact_bytes,
            retention_days=settings.artifact_retention_days,
        )
        return BoundArtifactWriter(
            service,
            context=ctx,
            sessionmaker=sessionmaker,
            store=store,
            principal=principal,
            session_id=ctx.session_id,
            run_id=run_id,
        )

    return _factory


def build_chat_runtime(
    *,
    mode: ChatCompositionMode,
    principal: Principal,
    backplane: Backplane,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    gateway: LLMGateway,
    request_id: str,
    source_ip: str,
    options: ChatCompositionOptions | None = None,
) -> ChatRuntime:
    """Build the ``ChatRuntime`` for one answer surface — the only builder (#576).

    ``mode`` selects the deliberate per-surface seams in the module table;
    everything else (model routing, fallback validation, context budget)
    is identical across modes. Interactive deadlines/suggestions keep
    their current values — budgets are another lane's scope.
    """
    opts = options or ChatCompositionOptions()
    interactive = mode == "interactive"
    live_writes = mode in ("interactive", "headless")

    context_kwargs: dict[str, Any] = {
        "fallback_max_input_tokens": settings.context_fallback_max_input_tokens,
        "output_headroom_tokens": settings.context_output_headroom_tokens,
        "compaction_digest_chars": settings.context_compaction_digest_chars,
        "compaction_chunk_size": settings.context_compaction_chunk_size,
    }
    if interactive:
        # #491: proactive compaction only on the interactive path today.
        context_kwargs["proactive_compaction_enabled"] = (
            settings.context_proactive_compaction_enabled
        )

    kwargs: dict[str, Any] = {
        "sessionmaker": sessionmaker,
        "gateway": gateway,
        "backplane": backplane,
        "principal": principal,
        "request_id": request_id,
        "source_ip": source_ip,
        "default_max_tool_turns": settings.chat_max_tool_turns,
        # Assistant Test is HARDCODED serial: its sessionmaker routes every
        # open to one shared rollback-only session (see assistant test).
        "tool_concurrency": 1 if mode == "test" else settings.chat_tool_concurrency,
        "fallback_model_validator": lambda vsession, mid: ChatModelService(
            settings, session=vsession, tenant_id=principal.tenant_id
        ).is_allowed_model_async(mid),
        "model_route_resolver": build_model_route_resolver(
            settings=settings,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            roles=principal.roles,
            request_id=request_id,
            source_ip=source_ip,
        ),
        "context_config": ContextConfig(**context_kwargs),
        "sandbox_factory": (
            build_sandbox_factory(
                principal=principal,
                backplane=backplane,
                settings=settings,
                sessionmaker=sessionmaker,
                request_id=request_id,
                source_ip=source_ip,
                object_store_factory=opts.object_store_factory,
            )
            if live_writes
            else None
        ),
        "artifact_writer_factory": (
            build_artifact_writer_factory(
                principal=principal,
                settings=settings,
                request_id=request_id,
                source_ip=source_ip,
                sessionmaker=sessionmaker,
                run_id=opts.run_id,
                object_store_factory=opts.object_store_factory,
            )
            if live_writes
            else None
        ),
        "mcp_tools_factory": (
            build_mcp_tools_factory(
                principal=principal,
                settings=settings,
                request_id=request_id,
                source_ip=source_ip,
            )
            if live_writes
            else None
        ),
        "interactive": interactive,
    }
    if interactive:
        kwargs.update(
            suggestions_enabled=settings.chat_suggestions_enabled,
            suggestions_count=settings.chat_suggestions_count,
            suggestions_timeout_seconds=settings.chat_suggestions_timeout_seconds,
            suggestions_grace_seconds=settings.chat_suggestions_grace_seconds,
            suggestions_model=settings.chat_suggestions_model,
            text_coalesce_chars=settings.chat_text_coalesce_chars,
            text_coalesce_seconds=settings.chat_text_coalesce_seconds,
            turn_timeout_seconds=settings.llm_interactive_timeout_seconds,
            interactive_max_attempts=settings.llm_interactive_max_attempts,
            terminal_publish_margin_seconds=settings.llm_terminal_publish_margin_seconds,
            answer_max_tokens=settings.chat_answer_max_tokens,
        )
    kwargs.update(opts.extra_runtime_kwargs)
    factory = opts.runtime_factory or ChatRuntime
    return factory(**kwargs)


__all__ = [
    "BoundArtifactWriter",
    "ChatCompositionMode",
    "ChatCompositionOptions",
    "ObjectStoreFactory",
    "build_artifact_writer_factory",
    "build_chat_runtime",
    "build_mcp_tools_factory",
    "build_sandbox_factory",
]
