"""The agentic, grounded RAG answer runtime (CC-6 #24 / CC-11 #26).

This is the **answer path**: given a persisted user message, it gives the
selected model the #45 retrieval tools, lets the model decide when to search,
grounds the answer in the permitted passages those searches return, streams the
answer + tool/citation events over the WS backplane, and persists the assistant
message with its citations (INV-3) and the audit trail (INV-6).

Layering (ADR-0004): orchestration only. It composes — never *is* — its
collaborators:

* the #36 ``llm/`` gateway (``stream_tools``) — the only model caller;
* the #45 ``retrieval/`` service tools (``search_text`` / ``search_documents`` /
  ``get_document``) — the only retrieval path, permission-filtered inside (INV-2);
* the ``realtime/`` backplane — the only pub/sub; the producer here publishes
  envelopes a decoupled WS consumer relays;
* the ``db/`` message + citation repositories (the only SQL) and the #23 audit
  sink (the only audit path).

**Grounding / INV-3 is structural, not prompt-only.** Citations are built *only*
from passages the retrieval tools actually returned (``GroundedCitation`` is
constructed from a ``RetrievedPassage``), so a citation can never reference a
passage the user could not retrieve, and the model cannot conjure one. A turn
that retrieved nothing yields a zero-citation answer, shown honestly as such —
the runtime prefers "I couldn't find it" over a confident, uncited answer.

**Lifecycle.** It publishes exactly the contract envelope sequence with a
monotonic ``seq``: ``start`` → ( ``delta`` | ``event:tool_call`` |
``event:tool_result`` | ``event:citation`` )* → exactly one terminal ``done`` |
``error``. Cancellation (client gone / shutdown) and any error both end the
stream with one terminal envelope and never leak a vendor error.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    CitationRepository,
    MessageRepository,
    TenantRepository,
    ToolInvocationRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.chat import GroundedCitation
from app.domain.entities import AuditOutcome, AutonomyLevel, MessageRole
from app.domain.llm import ChatMessage, Role, TokenUsage, ToolCall, ToolSpec
from app.domain.tools import ToolResult
from app.llm import LLMGateway
from app.realtime import envelopes
from app.realtime.backplane import Backplane
from app.retrieval import RetrievalService
from app.services.assistant_runtime import AssistantRunConfig, prepend_user_instructions
from app.services.audit import AuditSink
from app.services.autonomy_policy_service import AutonomyPolicyReader
from app.services.prompts import GROUNDED_SYSTEM_PROMPT, NO_SOURCES_FALLBACK
from app.services.provider_models import ModelRoute, ModelRouteResolver
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.impls import retrieval as _retrieval_impl
from app.services.tools.impls.run_python import RUN_PYTHON_TOOL_NAME
from app.services.tools.mcp_bridge import is_mcp_tool_name
from app.services.tools.registry import default_allowlist, tool_specs
from app.services.tools.runner import ToolRunner
from app.services.tools.types import SandboxToolRunner, ToolContext, ToolDefinition

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SandboxContext:
    """The per-answer inputs the sandbox seam factory needs (issue #231).

    Threaded from the runtime into the injected :data:`SandboxFactory` so the seam
    can create + link the ``code_run`` (the runtime's own ``session`` + the parent
    chat ``session_id`` / assistant ``message_id``), stream over the answer's
    ``stream_id`` with the shared monotonic ``next_seq`` (so ``code_output`` /
    ``code_result`` interleave with the answer's envelopes), and carry the audit
    correlation. Everything is the runtime's — never model/tool input.
    """

    session: AsyncSession
    stream_id: str
    session_id: UUID
    message_id: UUID
    next_seq: Callable[[], int]


# The injected code-execution seam factory (issue #231): given the per-answer
# :class:`SandboxContext`, build the :class:`SandboxToolRunner` the ``run_python``
# tool submits through, or ``None`` when code execution is unavailable (no live
# runner configured / disabled deploy). The runtime only calls it when the run's
# allow-list offers ``run_python``, so the seam is never built for a session that
# cannot use it.
SandboxFactory = Callable[["SandboxContext"], SandboxToolRunner | None]


# The injected MCP-tools resolver (issue #227): given the runtime's own DB session,
# return the caller's tenant-scoped registered+enabled MCP tools as governed CC-A
# :class:`ToolDefinition`s (namespaced ``mcp:<slug>:<tool>``), keyed by name. The
# runtime calls it **only** when the run's allow-list names at least one ``mcp:*``
# tool (deny-by-default: an ad-hoc / native-only run never touches the MCP servers
# table or opens a client). ``None`` (the default / offline case) ⇒ no MCP tools
# are resolved, so a stray ``mcp:*`` call is an ordinary ``tool_not_found``.
McpToolsFactory = Callable[[AsyncSession], Awaitable[dict[str, ToolDefinition]]]


# How many tool-calling turns the agent may take before the runtime forces a
# final answer (bounds the loop — no unbounded multi-step planning, issue #24
# OUT). Each turn is one streamed completion that may request tools. This module
# default mirrors ``Settings.chat_max_tool_turns`` and is the fallback when no
# configured default is injected (offline tests); the API always passes the
# configured value, which a tenant admin may override per tenant (issue #148).
_DEFAULT_MAX_TOOL_TURNS = 20
# Default passages per search (configurable budget; kept small for the MVP).
_DEFAULT_K = 6


@dataclass(slots=True)
class _StreamState:
    """Mutable per-stream bookkeeping (seq counter + accumulated answer)."""

    stream_id: str
    seq: int = 0
    # Set once a terminal (``done``/``error``) has been published, so the
    # exactly-one-terminal contract holds even when two terminal paths race —
    # e.g. an error already emitted and then a shutdown ``CancelledError`` tries
    # to emit its own (issue #156).
    terminal_sent: bool = False

    def next_seq(self) -> int:
        s = self.seq
        self.seq += 1
        return s


@dataclass(slots=True)
class _Usage:
    """Mutable running token usage across the turns of one answer."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: TokenUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens


class ChatRuntime:
    """Runs one grounded answer and streams it over the backplane.

    Constructed per answer with the collaborators it composes. The single entry
    point is :meth:`run`, intended to be launched as a background task off the
    202 send handler; it owns its own DB session (the request session is gone by
    then), commits the assistant turn, and publishes the terminal envelope.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: LLMGateway,
        backplane: Backplane,
        principal: Principal,
        request_id: str,
        source_ip: str,
        default_max_tool_turns: int = _DEFAULT_MAX_TOOL_TURNS,
        retrieval_factory: Callable[[AsyncSession], RetrievalService] | None = None,
        sandbox_factory: SandboxFactory | None = None,
        mcp_tools_factory: McpToolsFactory | None = None,
        model_route_resolver: ModelRouteResolver | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._gateway = gateway
        self._backplane = backplane
        self._principal = principal
        self._request_id = request_id
        self._source_ip = source_ip
        # System default tool-turn budget (``Settings.chat_max_tool_turns``); a
        # tenant's ``max_tool_turns`` override beats it when set (issue #148).
        self._default_max_tool_turns = default_max_tool_turns
        # The retrieval service is built per-answer over the runtime's own
        # session. Injectable so the offline tests supply a fake whose
        # ``search_text`` does not need pgvector; defaults to the real adapter.
        self._retrieval_factory = retrieval_factory or (
            lambda session: RetrievalService(session, gateway=self._gateway)
        )
        # The #231 code-execution seam factory. It is wired into the tool context
        # **only** when the run's allow-list offers ``run_python`` (below) — a
        # session that cannot run code never carries execution plumbing (no untested
        # seam on a path that does not use it). ``None`` (the default, and the offline
        # / no-code case) ⇒ ``run_python`` invocations report a typed ``ok=False``
        # rather than executing. The API wires the live factory (issue #231).
        self._sandbox_factory = sandbox_factory
        # The #227 MCP-tools resolver. Called ONLY when a run's allow-list names an
        # ``mcp:*`` tool (below) — an ad-hoc / native-only run never resolves MCP
        # tools (deny-by-default; no client/DB work on a path that cannot use them).
        # ``None`` (the default / offline case) means no MCP tools, so a stray
        # ``mcp:*`` call is an ordinary ``tool_not_found``. The API wires the live
        # resolver (per-tenant, per-owner, SSRF-guarded via mcp_servers_service).
        self._mcp_tools_factory = mcp_tools_factory
        # The PR-2a per-tenant model-route resolver. Called ONCE per answer (below)
        # to resolve the chosen model id to its gateway route: for a ``provider:`` id
        # it loads the tenant's provider, decrypts the key ONCE, and returns the raw
        # model id + that provider's api_key/base_url; for a config id it is a plain
        # passthrough. ``None`` (the default / offline case) ⇒ the model id is routed
        # as-is with default credentials (a config-only runtime). The API wires the
        # live resolver (per-tenant, holding the CC-C secrets vault).
        self._model_route_resolver = model_route_resolver

    async def run(
        self,
        *,
        stream_id: str,
        session_id: UUID,
        question: str,
        model: str,
        history: Sequence[ChatMessage],
        collection_ids: list[UUID] | None,
        assistant_config: AssistantRunConfig | None = None,
        custom_instructions: str | None = None,
        simulate_writes: bool = False,
    ) -> None:
        """Produce the grounded answer for ``stream_id`` end-to-end.

        Publishes ``start``, runs the agentic tool loop (streaming ``delta`` and
        ``event`` envelopes), persists the assistant message + citations, then
        publishes exactly one terminal ``done``. Any error is mapped to a typed
        problem and published as the terminal ``error`` envelope (the vendor
        error never escapes). The assistant message id is minted up front so it
        rides ``start`` and is the row the citations attach to.

        When ``assistant_config`` is set (a session started from an assistant,
        ADR-0011 §2) the run uses its instructions-augmented system prompt, its
        allowed-tool subset, and its knowledge scope as an **additional narrowing**
        filter over any send-time ``collection_ids`` (scope may only narrow, never
        widen — INV-2). ``assistant_config=None`` is ad-hoc chat, unchanged.

        ``custom_instructions`` is the asking user's per-account preamble (from their
        preferences). When set it is prepended to the system prompt — before any
        assistant instructions and always before ``GROUNDED_SYSTEM_PROMPT`` — so it can
        shape persona/tone but never removes the grounding/citation rules (INV-3). It is
        resolved by the caller (which holds the principal + DB session), never read from
        inside the gateway.

        ``simulate_writes`` is the read-only test/preview seam (F-AB-5, issue #215):
        when set, the tool context carries ``simulate_writes=True``, so a T1
        file-writing tool builds + validates the bytes but persists **nothing** — the
        write is simulated, not executed. The code-execution seam is independently
        left unwired for a test run (``sandbox=None``), so ``run_python`` reports a
        typed ``ok=False`` rather than launching a container. A test run therefore
        performs NO real side effect (the load-bearing property of the harness).
        """
        state = _StreamState(stream_id=stream_id)
        assistant_message_id = uuid.uuid4()
        await self._publish(
            state,
            envelopes.start(
                stream_id,
                state.next_seq(),
                data={
                    "sessionId": str(session_id),
                    "messageId": str(assistant_message_id),
                    "model": model,
                },
            ),
        )
        try:
            async with self._sessionmaker() as session:
                # Bind the RLS GUC on the runtime's own session for this
                # transaction (#17): the answer producer runs off the request
                # path with a fresh session, so it arms the Postgres RLS backstop
                # itself, keyed off the streaming principal's tenant. No-op off
                # Postgres (offline tests).
                await bind_tenant(session, self._principal.tenant_id)
                result = await self._answer(
                    session=session,
                    state=state,
                    session_id=session_id,
                    assistant_message_id=assistant_message_id,
                    question=question,
                    model=model,
                    history=history,
                    collection_ids=collection_ids,
                    assistant_config=assistant_config,
                    custom_instructions=custom_instructions,
                    simulate_writes=simulate_writes,
                )
                await session.commit()
        except asyncio.CancelledError:
            # Shutdown / client-gone cancellation (``main._drain_answer_tasks``
            # cancels every in-flight producer on SIGTERM, issue #156). In Python
            # 3.12 ``CancelledError`` is a ``BaseException``, so it bypasses the
            # ``AppError``/``Exception`` arms below — without this arm the stream
            # would end after ``start`` with NO terminal, and the Redis replay
            # buffer would hand a reconnecting client an orphaned stream to wait
            # on forever. Emit one RETRYABLE terminal (503; the contract has the
            # client treat ``error`` as retryable) then RE-RAISE so the task still
            # stops — never swallow cancellation.
            await self._terminal_error(
                state,
                503,
                "Service Unavailable",
                "dependency_unavailable",
                "Server is shutting down.",
            )
            raise
        except AppError as exc:
            await self._terminal_error(state, exc.status, exc.title, exc.code, exc.detail)
            return
        except Exception as exc:  # noqa: BLE001 — never leak a vendor error to the client
            # Log the error *type* only (never the message — it may carry vendor
            # details / a key). The client gets an opaque 500 problem envelope.
            log.error("chat_runtime.failed", stream_id=stream_id, error_type=type(exc).__name__)
            await self._terminal_error(state, 500, "Internal Server Error", "internal_error", None)
            return

        if state.terminal_sent:
            # A terminal already fired (e.g. cancellation mid-commit raced ahead);
            # never publish a second terminal — exactly-one-terminal contract.
            return
        state.terminal_sent = True
        await self._publish(
            state,
            envelopes.done(
                stream_id,
                state.next_seq(),
                data={
                    "messageId": str(assistant_message_id),
                    "finishReason": result.finish_reason,
                    "citationCount": len(result.citations),
                    "usage": {
                        "promptTokens": result.prompt_tokens,
                        "completionTokens": result.completion_tokens,
                        "totalTokens": result.total_tokens,
                    },
                },
            ),
        )

    # --- the agentic loop ---------------------------------------------------

    async def _answer(
        self,
        *,
        session: AsyncSession,
        state: _StreamState,
        session_id: UUID,
        assistant_message_id: UUID,
        question: str,
        model: str,
        history: Sequence[ChatMessage],
        collection_ids: list[UUID] | None,
        assistant_config: AssistantRunConfig | None = None,
        custom_instructions: str | None = None,
        simulate_writes: bool = False,
    ) -> _RunResult:
        """The tool-calling loop: search → ground → stream → persist."""
        tenant_id = self._principal.tenant_id
        retrieval = self._retrieval_factory(session)
        audit = AuditSink(AuditEventRepository(session, tenant_id))
        # Resolve the chosen model's gateway route ONCE for the whole answer (PR 2a):
        # for a per-tenant ``provider:`` id this loads the provider + decrypts its key
        # a single time and yields the raw model id + api_key/base_url; for a config
        # id it is a plain passthrough with default credentials. The SAME resolved
        # route (and decrypted key) is reused across every gateway call of the turn
        # loop below — the key is never re-decrypted per call, nor logged.
        route = await self._resolve_model_route(session, model)
        # The per-run allow-list (issue #207 §2): for ad-hoc chat this is the
        # default read-only retrieval tools; a session started from an assistant
        # (E1) narrows/selects it from the registry (ADR-0011 §2). The governed
        # runner is the single chokepoint every tool call passes through — it
        # enforces the allow-list + approval seam, bounds each call, records a
        # ``tool_invocations`` row, and emits ``tool.invoked``/``tool.result``
        # (CC-7 / INV-6). Off-list / failing tools become results, not crashes.
        allowed = (
            assistant_config.allowed if assistant_config is not None else default_allowlist()
        )
        # The tenant's registered+enabled MCP tools (issue #227), resolved per-run
        # (never a global registration — they are tenant-scoped and dynamic, so a
        # cross-tenant leak is impossible; INV-1). Resolved ONLY when the allow-list
        # names an ``mcp:*`` tool, so an ad-hoc / native-only run never touches the
        # MCP servers table or opens a client. Each is a namespaced ``mcp:<slug>:
        # <tool>`` ``ToolDefinition`` whose handler invokes through the SSRF-guarded,
        # auth-resolving adapter; the runner then governs it on the SAME allow-list /
        # approval / audit path as a native tool.
        mcp_tools = await self._resolve_mcp_tools(session, allowed)
        # The assistant's knowledge scope is an ADDITIONAL narrowing filter over
        # any send-time collection_ids — it can only intersect (narrow), never widen
        # (INV-2). The per-user permission predicate still runs inside retrieval/,
        # keyed off the running principal.
        effective_collection_ids = _narrow_collection_ids(
            collection_ids,
            assistant_config.collection_ids if assistant_config is not None else None,
        )
        # The system prompt: the assistant's instructions-augmented grounded prompt
        # when a config is present (grounding/citation rules preserved), else the
        # bare grounded prompt (ad-hoc chat, unchanged). The asking user's custom
        # instructions (from their preferences) are then prepended so the final order
        # is: user custom instructions → (assistant instructions) → GROUNDED_SYSTEM_PROMPT.
        # Grounding is NEVER removed — it always follows (INV-3).
        base_prompt = (
            assistant_config.system_prompt
            if assistant_config is not None
            else GROUNDED_SYSTEM_PROMPT
        )
        system_prompt = prepend_user_instructions(custom_instructions, base_prompt)
        # The run's EFFECTIVE autonomy (issue #218): for an assistant session, the
        # assistant's configured level min'd to the tenant admin cap; for ad-hoc chat
        # (no assistant) there is no assistant to gate, and the default set is all-T0,
        # so ACT_AUTO leaves ad-hoc behaviour unchanged. The runner gates side-effecting
        # T1 tools by this level (suggest/draft ⇒ refused, act_with_approval ⇒ routed
        # through the approval seam, act_auto ⇒ executed automatically). The cap is
        # tenant-scoped (INV-1) — keyed off the running principal's tenant.
        effective_autonomy = await self._resolve_effective_autonomy(
            session, tenant_id, assistant_config
        )
        runner = ToolRunner(
            allowed=allowed,
            invocations=ToolInvocationRepository(session, tenant_id),
            audit=audit,
            actor=AuditActor.user(self._principal.user_id),
            request_id=self._request_id,
            source_ip=self._source_ip,
            session_id=session_id,
            # The policy-driven approval gate (issue #223) — replaces the inert
            # DenyAllApprovalGate on the live path. A requires_approval tool (⇒ T2+)
            # executes ONLY if the tenant's admin policy has enabled AND pre-approved
            # it (enabled=true, requires_approval=false); otherwise denied. Fail-closed
            # if the policy can't be read. This is what lets an admin turn on
            # run_python for their tenant. Tenant-scoped: keyed off the running
            # principal's tenant, never request input (INV-1).
            gate=PolicyApprovalGate(session, tenant_id=tenant_id),
            extra_tools=mcp_tools,
            autonomy=effective_autonomy,
        )
        # The tool schemas advertised to the model — exactly the allow-list (native
        # + the resolved MCP tools it names), so the model is only *offered* tools it
        # may call (the runner still enforces the allow-list as the hard gate). An
        # allow-listed ``mcp:*`` tool whose server is disabled / unregistered simply
        # is not in ``mcp_tools`` and so is silently not offered (deny-by-default).
        advertised = tool_specs(allowed) + _mcp_tool_specs(mcp_tools, allowed)
        # The loop bound: this tenant's ``max_tool_turns`` override if set, else
        # the configured system default (issue #148).
        max_tool_turns = await self._resolve_max_tool_turns(session, tenant_id)

        messages: list[ChatMessage] = [
            ChatMessage(role=Role.SYSTEM, content=system_prompt),
            *history,
            ChatMessage(role=Role.USER, content=question),
        ]

        # Citations keyed by chunk_id so the same passage cited across turns is
        # recorded once (INV-3 set), preserving first-seen order.
        cited: dict[UUID, GroundedCitation] = {}
        # The answer is the text of exactly ONE turn: the tool-free turn the model
        # reaches naturally, or the forced synthesis below. A tool-CALLING turn's
        # text is pre-tool narration ("I'll search…"), not answer content — it is
        # never streamed as a ``delta`` nor persisted (issue #148). Streaming it
        # would diverge the live answer from the stored message and re-expose the
        # narration-as-answer bug for clients that render the stream.
        answer_chunks: list[str] = []
        usage = _Usage()
        finish_reason = "stop"
        total_hits = 0

        # Run the agentic tool loop. ``budget_exhausted`` stays True only if every
        # turn within the budget requested tools — i.e. the model never reached a
        # tool-free answer turn (the loop never ``break``s). That is the case the
        # forced synthesis below repairs (issue #148).
        # The permission-scoped context every tool handler runs against (issue
        # #207): it carries the asking principal (the ``retrieval/`` filter keys
        # off it, INV-2) and the run's retrieval service + collection scope. Built
        # once — the handlers reach nothing outside it.
        #
        # The #231 code-execution seam is wired in ONLY when this run's allow-list
        # offers ``run_python`` AND a live factory is configured — a session that
        # cannot run code carries no execution plumbing (deny-by-default; no untested
        # seam on a path that does not use it). ``run_python`` is off the ad-hoc
        # default allow-list and admin-gated, so ad-hoc chat never wires it.
        sandbox = self._build_sandbox_seam(
            session=session,
            state=state,
            allowed=allowed,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
        )
        tool_context = ToolContext(
            principal=self._principal,
            retrieval=retrieval,
            collection_ids=effective_collection_ids,
            default_k=_DEFAULT_K,
            session_id=session_id,
            sandbox=sandbox,
            # Read-only test/preview mode (F-AB-5, issue #215): a T1 file-writing tool
            # builds + validates but persists nothing, so a test run mutates no state.
            # ``run_python`` (T2) is already denied for a test run because the sandbox
            # seam is left unwired above (``sandbox=None``), so no container launches.
            simulate_writes=simulate_writes,
        )

        budget_exhausted = True
        for _turn in range(max_tool_turns):
            turn_tool_calls, finish_reason, turn_text = await self._stream_one_turn(
                messages=messages, route=route, usage=usage, tools=advertised
            )
            if not turn_tool_calls:
                # Tool-free turn → this is the answer. Only now is its text known
                # to be answer content (not narration), so stream it now and stop.
                await self._publish_text(state, turn_text)
                answer_chunks = turn_text
                budget_exhausted = False
                break

            # The assistant turn that requested tools must be in the transcript
            # before its tool results (provider protocol). Its narration text is
            # dropped — neither streamed nor persisted.
            messages.append(
                ChatMessage(role=Role.ASSISTANT, content="", tool_calls=tuple(turn_tool_calls))
            )
            for call in turn_tool_calls:
                result = await self._run_one_tool(
                    state=state,
                    runner=runner,
                    audit=audit,
                    context=tool_context,
                    call=call,
                    message_id=assistant_message_id,
                )
                total_hits += result.hit_count
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        content=result.content,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                # Record + emit citations for each newly-seen permitted passage.
                for passage in result.passages:
                    if passage.chunk_id in cited:
                        continue
                    citation = GroundedCitation.from_passage(passage)
                    cited[passage.chunk_id] = citation

        if budget_exhausted:
            # The whole budget went to tool turns and the model never volunteered a
            # tool-free answer (issue #148 — the live empty-answer bug). Force one
            # synthesis turn with tools disabled so the model answers over the
            # gathered tool context, then stream + persist THAT as the answer. The
            # narration from the exhausted tool turns was never emitted, so the
            # live stream and the stored message agree.
            _, finish_reason, turn_text = await self._stream_one_turn(
                messages=messages, route=route, usage=usage, tools=advertised, tool_choice="none"
            )
            await self._publish_text(state, turn_text)
            answer_chunks = turn_text

        answer_text = "".join(answer_chunks).strip()

        # Persist the assistant message and its citations (INV-3): the citations
        # are exactly the permitted passages the tools returned — never more.
        if not answer_text:
            # Still no answer text — even a forced synthesis said nothing (e.g.
            # retrieval found nothing). Fall back to an honest "couldn't find it"
            # rather than persisting an empty turn.
            answer_text = NO_SOURCES_FALLBACK
            await self._publish(
                state,
                envelopes.delta(state.stream_id, state.next_seq(), {"text": answer_text}),
            )

        stored_citations = await self._persist(
            session=session,
            tenant_id=tenant_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            model=model,
            content=answer_text,
            citations=list(cited.values()),
        )
        # Emit a citation event per persisted citation (now carrying its row id).
        # ``cited`` is already deduplicated by chunk_id, so each stored citation is
        # a distinct permitted passage — one event each, no extra guard needed.
        for citation in stored_citations:
            await self._emit_citation(state, citation)

        await self._audit_answer(
            audit=audit,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            question=question,
            model=model,
            citation_count=len(stored_citations),
            retrieved_hits=total_hits,
            # Distinct cited documents, first-appearance order — the provenance
            # the Audit "Answers cited" KPI reads (#249).
            cited_document_ids=list(
                dict.fromkeys(str(c.document_id) for c in stored_citations)
            ),
        )

        return _RunResult(
            finish_reason=finish_reason,
            citation_count=len(stored_citations),
            citations=tuple(stored_citations),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )

    async def _resolve_mcp_tools(
        self, session: AsyncSession, allowed: frozenset[str]
    ) -> dict[str, ToolDefinition]:
        """The run's tenant-scoped MCP tools, resolved only if the allow-list names one.

        Deny-by-default + no-untested-plumbing (mirrors the ``run_python`` seam): the
        resolver is invoked **only** when a live factory is wired *and* the run's
        allow-list actually names at least one ``mcp:*`` tool. An ad-hoc / native-
        only run never touches the MCP servers table or opens a client. The resolver
        itself lists only the caller's own enabled servers in this tenant
        (INV-1/INV-2), so a disabled / foreign server contributes nothing. Any
        resolver failure is contained as "no MCP tools" — a broken MCP registry must
        never break an otherwise-answerable chat run.
        """
        if self._mcp_tools_factory is None:
            return {}
        if not any(is_mcp_tool_name(name) for name in allowed):
            return {}
        try:
            return await self._mcp_tools_factory(session)
        except Exception:  # noqa: BLE001 — a broken MCP registry must not break the run
            log.warning("chat_runtime.mcp_resolve_failed")
            return {}

    def _build_sandbox_seam(
        self,
        *,
        session: AsyncSession,
        state: _StreamState,
        allowed: frozenset[str],
        session_id: UUID,
        assistant_message_id: UUID,
    ) -> SandboxToolRunner | None:
        """Build the ``run_python`` code-execution seam, or ``None`` (issue #231).

        Deny-by-default + no-untested-plumbing: the seam is built **only** when this
        run's allow-list actually offers ``run_python`` *and* a live sandbox factory
        was injected. Ad-hoc chat (``run_python`` off the default allow-list) and any
        session whose assistant did not grant it never carry the seam, so the
        ``ToolContext.sandbox`` stays ``None`` and a stray ``run_python`` invocation
        reports a typed ``ok=False`` instead of executing. The factory itself may
        also return ``None`` (no runner configured / disabled deploy) — same result.
        """
        if self._sandbox_factory is None or RUN_PYTHON_TOOL_NAME not in allowed:
            return None
        return self._sandbox_factory(
            SandboxContext(
                session=session,
                stream_id=state.stream_id,
                session_id=session_id,
                message_id=assistant_message_id,
                next_seq=state.next_seq,
            )
        )

    async def _resolve_max_tool_turns(self, session: AsyncSession, tenant_id: UUID) -> int:
        """The effective tool-turn budget for this answer (issue #148).

        A tenant admin may override the system default per tenant
        (``Tenant.max_tool_turns``); ``None`` ⇒ the configured default this
        runtime was built with (``Settings.chat_max_tool_turns``). Clamped to
        ≥ 1 so the loop always runs at least one turn against a degenerate value.
        """
        tenant = await TenantRepository(session).get(tenant_id)
        override = tenant.max_tool_turns if tenant is not None else None
        return max(1, override if override is not None else self._default_max_tool_turns)

    async def _resolve_effective_autonomy(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        assistant_config: AssistantRunConfig | None,
    ) -> AutonomyLevel:
        """The EFFECTIVE autonomy for this answer — the assistant's level min'd to the cap.

        Ad-hoc chat (no assistant) has no assistant to gate and offers only T0 tools,
        so it runs at ``ACT_AUTO`` (the autonomy gate is inert there). An assistant
        session runs at ``min(assistant.autonomy, tenant cap)`` (issue #218) — the
        tenant admin cap can only LOWER an assistant's configured level. Tenant-scoped
        (INV-1): the cap is keyed off the running principal's tenant, never request
        input.
        """
        if assistant_config is None:
            return AutonomyLevel.ACT_AUTO
        return await AutonomyPolicyReader(session, tenant_id=tenant_id).clamp(
            assistant_config.autonomy
        )

    async def _resolve_model_route(self, session: AsyncSession, model: str) -> ModelRoute:
        """Resolve the chosen model id to its gateway route ONCE per answer (PR 2a).

        For a per-tenant ``provider:`` id the injected resolver loads the provider
        (must be in the tenant + enabled) and decrypts its key a single time,
        returning the raw model id + that provider's api_key/base_url; for a config
        id it is a plain passthrough (default credentials). ``None`` resolver (the
        offline / config-only case) ⇒ the id is routed as-is with default
        credentials — so an offline runtime keeps working unchanged. The send path
        has already validated a ``provider:`` id against the tenant (422 otherwise),
        so by here it is known-good; a resolver returning the plain model for an
        unexpected id degrades safely to default credentials rather than crashing.
        """
        if self._model_route_resolver is None:
            return ModelRoute(model=model)
        return await self._model_route_resolver(session, model)

    async def _stream_one_turn(
        self,
        *,
        messages: list[ChatMessage],
        route: ModelRoute,
        usage: _Usage,
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
    ) -> tuple[list[ToolCall], str, list[str]]:
        """Consume one completion turn; return ``(tool_calls, finish_reason, text)``.

        Buffers the turn's text chunks and folds token ``usage`` into the running
        total, but does **not** publish: only the caller knows whether a turn's
        text is answer content (a tool-free turn, or the forced synthesis) to be
        streamed, or pre-tool narration (a tool-calling turn) to be dropped (issue
        #148). ``tools`` is the run's allow-list rendered as ``ToolSpec``s (issue
        #207 §2 — the model is only offered tools it may call). ``tool_choice="none"``
        forces a tool-free turn — the final synthesis once the budget is spent.

        ``route`` carries the answer's resolved gateway route (PR 2a): the raw model
        id + (for a per-tenant provider) the api_key/base_url override. The SAME
        route is passed on every turn of the loop, so a provider's decrypted key is
        reused, never re-decrypted per turn.
        """
        turn_tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        text_chunks: list[str] = []
        async for ev in self._gateway.stream_tools(
            messages,
            tools=list(tools),
            model=route.model,
            tool_choice=tool_choice,
            api_key=route.api_key,
            api_base=route.api_base,
        ):
            if ev.text:
                text_chunks.append(ev.text)
            if ev.tool_calls:
                turn_tool_calls = list(ev.tool_calls)
            if ev.finish_reason:
                finish_reason = ev.finish_reason
            if ev.usage is not None:
                usage.add(ev.usage)
        return turn_tool_calls, finish_reason, text_chunks

    async def _publish_text(self, state: _StreamState, chunks: list[str]) -> None:
        """Publish answer text as ``delta`` envelopes, preserving chunk granularity.

        Called only for the answer turn's text — the tool-free turn or the forced
        synthesis — never for a tool-calling turn's pre-tool narration (issue
        #148), so the streamed answer equals the persisted one.
        """
        for chunk in chunks:
            if chunk:
                await self._publish(
                    state, envelopes.delta(state.stream_id, state.next_seq(), {"text": chunk})
                )

    async def _run_one_tool(
        self,
        *,
        state: _StreamState,
        runner: ToolRunner,
        audit: AuditSink,
        context: ToolContext,
        call: ToolCall,
        message_id: UUID,
    ) -> ToolResult:
        """Surface a tool_call event, run the call through the governed runner, emit its result.

        The runner is the single governance chokepoint (issue #207): it enforces
        the allow-list + approval seam, bounds the call, records the
        ``tool_invocations`` row, and emits ``tool.invoked``/``tool.result`` — so
        an off-list / unapproved / failing tool returns an ``ok=False`` result the
        model reads, and the stream never crashes (AC-2/3/5, INV-6). This method
        only renders the WS trace envelopes and layers the retrieval-specific
        ``retrieval.query`` audit on top for tools that returned passages/documents.
        """
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="tool_call",
                data={"callId": call.id, "tool": call.name, "args": call.arguments},
            ),
        )
        result = await runner.run(call=call, context=context, message_id=message_id)
        # A retrieval tool additionally emits the retrieval-semantics audit event
        # (query hash + document ids + hit count, spec 0004 §2.4). The generic
        # ``tool.invoked``/``tool.result`` events the runner emits are additive.
        if _is_retrieval_call(call):
            await self._audit_retrieval(audit=audit, call=call, result=result)
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="tool_result",
                data={
                    "callId": call.id,
                    "tool": call.name,
                    "hitCount": result.hit_count,
                    "summary": result.summary,
                    "ok": result.ok,
                    **({"error": result.error} if result.error else {}),
                },
            ),
        )
        return result

    # --- persistence + audit ------------------------------------------------

    async def _persist(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        session_id: UUID,
        assistant_message_id: UUID,
        model: str,
        content: str,
        citations: list[GroundedCitation],
    ) -> list[GroundedCitation]:
        """Persist the assistant message + its citations; return citations w/ ids.

        The message row uses the pre-minted ``assistant_message_id`` (the same id
        that rode ``start``) so the WS ``messageId`` and the stored row agree. Each
        citation persists with the **source** char span (deep-link to the document,
        CC-11) and reloads carrying its row id for the citation events.
        """
        message_repo = MessageRepository(session, tenant_id)
        citation_repo = CitationRepository(session, tenant_id)
        await message_repo.add_with_id(
            message_id=assistant_message_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=content,
            model=model,
        )
        stored: list[GroundedCitation] = []
        for citation in citations:
            row = await citation_repo.add(
                message_id=assistant_message_id,
                chunk_id=citation.chunk_id,
                char_start=citation.char_start,
                char_end=citation.char_end,
                score=citation.score,
            )
            stored.append(
                GroundedCitation(
                    id=row.id,
                    document_id=citation.document_id,
                    document_name=citation.document_name,
                    chunk_id=citation.chunk_id,
                    snippet=citation.snippet,
                    char_start=citation.char_start,
                    char_end=citation.char_end,
                    score=citation.score,
                )
            )
        return stored

    async def _audit_retrieval(
        self, *, audit: AuditSink, call: ToolCall, result: ToolResult
    ) -> None:
        query = str(call.arguments.get("query") or call.arguments.get("name_or_query") or "")
        await audit.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="retrieval",
            resource_id=call.id,
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "tool": call.name,
                "query_hash": _hash_query(query),
                "document_ids": [str(d) for d in result.document_ids],
                "hit_count": result.hit_count,
            },
        )

    async def _audit_answer(
        self,
        *,
        audit: AuditSink,
        session_id: UUID,
        assistant_message_id: UUID,
        question: str,
        model: str,
        citation_count: int,
        retrieved_hits: int,
        cited_document_ids: Sequence[str],
    ) -> None:
        await audit.emit(
            action=AuditAction.ANSWER_GENERATED,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="message",
            resource_id=str(assistant_message_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "session_id": str(session_id),
                "model": model,
                "query_hash": _hash_query(question),
                "citation_count": citation_count,
                "retrieved_hits": retrieved_hits,
                # The distinct documents this answer grounded on. The audit read
                # path (audit_query_service) synthesises allow-candidates from
                # ``document_ids`` when an event has no explicit candidates, so
                # recording them here is what makes the Audit "Answers cited" KPI
                # count a grounded answer as cited (#249). Empty ⇒ no candidates
                # ⇒ honestly "not grounded".
                "document_ids": list(cited_document_ids),
            },
        )

    # --- envelope helpers ---------------------------------------------------

    async def _emit_citation(self, state: _StreamState, citation: GroundedCitation) -> None:
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="citation",
                data={
                    "id": str(citation.id),
                    "documentId": str(citation.document_id),
                    "documentName": citation.document_name,
                    "chunkId": str(citation.chunk_id),
                    "snippet": citation.snippet,
                    "charStart": citation.char_start,
                    "charEnd": citation.char_end,
                    **({"score": citation.score} if citation.score is not None else {}),
                },
            ),
        )

    async def _terminal_error(
        self, state: _StreamState, status: int, title: str, code: str, detail: str | None
    ) -> None:
        if state.terminal_sent:
            # A terminal already fired for this stream; never publish a second one
            # (exactly-one-terminal contract). Guards the case where an error and a
            # shutdown ``CancelledError`` both reach a terminal path (issue #156).
            return
        state.terminal_sent = True
        problem: dict[str, object] = {"title": title, "status": status, "code": code}
        if detail:
            problem["detail"] = detail
        await self._publish(state, envelopes.error(state.stream_id, state.next_seq(), problem))

    async def _publish(self, state: _StreamState, envelope: dict[str, object]) -> None:
        await self._backplane.publish(state.stream_id, envelope)


@dataclass(frozen=True, slots=True)
class _RunResult:
    """Internal result of the answer loop (feeds the ``done`` envelope)."""

    finish_reason: str
    citation_count: int
    citations: tuple[GroundedCitation, ...]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def _mcp_tool_specs(
    mcp_tools: dict[str, ToolDefinition], allowed: frozenset[str]
) -> tuple[ToolSpec, ...]:
    """Render the allow-listed MCP tools to the ``ToolSpec``s advertised to the model.

    Only tools that are BOTH resolved (a registered, enabled server of the caller's)
    AND in the run's allow-list are offered — the same restriction ``tool_specs``
    applies to native tools, so the model is offered exactly what it may call. The
    runner still enforces the allow-list as the hard chokepoint. Deterministically
    ordered by name so the offered set is stable.
    """
    return tuple(
        ToolSpec(
            name=defn.name,
            description=defn.description,
            parameters=defn.json_schema,
        )
        for name, defn in sorted(mcp_tools.items())
        if name in allowed
    )


def _narrow_collection_ids(
    send_ids: list[UUID] | None, assistant_ids: list[UUID] | None
) -> list[UUID] | None:
    """Intersect the send-time and assistant scopes — narrow only, never widen (INV-2).

    An assistant's ``knowledge_scope`` is an *additional narrowing* filter over any
    per-send ``collection_ids``. The rules (each a narrowing):

    * neither set ⇒ ``None`` (no collection filter — ad-hoc default);
    * only one set ⇒ that set (it narrows from "all permitted" to those ids);
    * both set ⇒ their **intersection** (the assistant can only remove collections
      the send named, never add ones outside its own scope).

    A both-set intersection that is empty stays an **empty list** (not ``None``):
    that is the strictest narrowing — "no collection is in both scopes" must return
    nothing, never fall back to the unfiltered set. The per-user permission filter
    inside ``retrieval/`` still runs on top, keyed off the running principal.
    """
    if assistant_ids is None:
        return send_ids
    if send_ids is None:
        return list(assistant_ids)
    allowed = set(assistant_ids)
    return [cid for cid in send_ids if cid in allowed]


def _hash_query(query: str) -> str:
    """A stable, non-reversible hash of the query (audit metadata, spec 0004 §2.4).

    The audit log records a query **hash**, not the raw query, so the trail does
    not duplicate potentially sensitive question text while still letting a
    reviewer correlate retrieval + answer events for the same turn.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _is_retrieval_call(call: ToolCall) -> bool:
    """Whether a tool call targets a retrieval tool (gets the ``retrieval.query`` audit).

    The three retrieval tools additionally emit the retrieval-semantics audit event
    (query hash + document ids + hit count, spec 0004 §2.4) on top of the generic
    ``tool.*`` events the runner emits for every tool. Keyed off the retrieval impl's
    declared names so a future non-retrieval tool does not wrongly get it.
    """
    return call.name in _RETRIEVAL_TOOL_NAMES


# The retrieval tools' names, read once from their impl module (the single source
# of truth for what a "retrieval tool" is) so the retrieval-specific audit stays
# correct as tools are added elsewhere.
_RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset(
    defn.name for defn in _retrieval_impl.TOOLS
)


__all__ = [
    "ChatRuntime",
    "McpToolsFactory",
    "ModelRoute",
    "ModelRouteResolver",
    "SandboxContext",
    "SandboxFactory",
]
