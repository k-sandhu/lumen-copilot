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
monotonic ``seq``: ``start`` → ( ``delta`` | ``event:step`` |
``event:tool_call`` | ``event:tool_result`` | ``event:citation`` |
``event:ask_user`` | ``event:suggestions`` )* → exactly one terminal ``done`` |
``error``. Cancellation (client gone / shutdown) and any error both end the
stream with one terminal envelope and never leak a vendor error.

**Spec 0006 (#429) affordances.** ``event:step`` marks run phases (``prepare`` /
``think`` / ``finalize`` / ``suggest``) — transient run-visibility state, never
persisted. A valid ``ask_user`` tool call ends the turn as a clarifying
question: the question persists as the assistant message (zero citations — a
question makes no claims), ``event:ask_user`` carries the options, and the
stream ends ``done(finishReason="ask_user")``; the user's choice arrives as an
ordinary next message. After a normal answer, one config-gated, time-bounded
completion proposes follow-up questions (``event:suggestions``); any failure is
a silent skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    CitationRepository,
    LlmUsageRepository,
    MessageRepository,
    TenantRepository,
    ToolInvocationRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.chat import AskUserQuestion, AskUserValidationError, GroundedCitation
from app.domain.entities import AuditOutcome, AutonomyLevel, MessageRole
from app.domain.llm import ChatMessage, Role, TokenUsage, ToolCall, ToolSpec
from app.domain.tools import ToolResult
from app.llm import LLMGateway
from app.llm.context import ContextConfig, assemble_context, fit_transcript
from app.realtime import envelopes
from app.realtime.backplane import Backplane
from app.retrieval import RetrievalService
from app.services.assistant_runtime import AssistantRunConfig, prepend_user_instructions
from app.services.audit import AuditSink
from app.services.autonomy_policy_service import AutonomyPolicyReader
from app.services.prompts import (
    FOLLOW_UP_SYSTEM_PROMPT,
    GROUNDED_SYSTEM_PROMPT,
    NO_SOURCES_FALLBACK,
    render_follow_up_request,
)
from app.services.provider_models import ModelRoute, ModelRouteResolver
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.impls import retrieval as _retrieval_impl
from app.services.tools.impls.ask_user import ASK_USER_TOOL_NAME
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
# How many of one turn's read-only tool calls may execute at once (#412,
# ADR-0016 §5). Each concurrently EXECUTING call briefly opens its own DB
# session (the scope closes before the call queues to persist), so the cap
# bounds the per-answer draw on the engine pool (the default pool is 5 +
# overflow; 4 executing scopes + the runtime session fit inside it). A value
# of 1 disables fan-out entirely — the genuinely serial pre-#412 path.
# Mirrors ``Settings.chat_tool_concurrency`` (validated [1, 16] there); this
# constant is the offline/test default.
_DEFAULT_TOOL_CONCURRENCY = 4
# The per-search passage budget now comes from the context assembler
# (``ContextBudget.retrieval_k``, ADR-0016 §1 / #410): the historical default of
# 6 when the window is roomy, fewer when it is tight — so the knob is derived
# from the input budget rather than a fixed constant here.

# Follow-up suggestions (spec 0006 #429) module defaults. Suggestions are
# OPT-IN at the constructor: only the interactive chat API turns them on (from
# ``Settings.chat_suggestions_enabled``), so the runtime's other consumers —
# headless runs, the assistant preview harness, offline tests — never pay for a
# nicety completion nobody reads. The prompt sees only the tail of a long
# answer (the part follow-ups anchor to) and a bounded question, so the nicety
# call stays cheap on any conversation.
_DEFAULT_SUGGESTIONS_ENABLED = False
_DEFAULT_SUGGESTIONS_COUNT = 3
_DEFAULT_SUGGESTIONS_TIMEOUT_SECONDS = 8.0
_SUGGESTIONS_ANSWER_TAIL_CHARS = 4000
_SUGGESTIONS_QUESTION_HEAD_CHARS = 1000
_SUGGESTIONS_MAX_TOKENS = 400
_SUGGESTION_MAX_CHARS = 200


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
    """Mutable running token usage across the turns of one answer.

    Carries the provider cache accounting too (#409, ADR-0016 §2.6):
    ``cached_prompt_tokens`` served from the provider's prompt cache and
    ``cache_write_tokens`` written into it — summed across turns exactly like
    the base counters, and zero for providers that report no cache detail.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    # The LAST answer-loop turn's prompt size — window occupancy, not billing
    # (#434 NEW-1). Set ONLY by ``_stream_one_turn`` (each loop turn overwrites,
    # so the final turn wins); the suggestions nicety folds into the sums via
    # :meth:`add` but never touches this. ``None`` ⇒ the provider reported no
    # usage.
    last_turn_prompt_tokens: int | None = None

    def add(self, usage: TokenUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.cached_prompt_tokens += usage.cached_prompt_tokens
        self.cache_write_tokens += usage.cache_write_tokens


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
        tool_concurrency: int = _DEFAULT_TOOL_CONCURRENCY,
        context_config: ContextConfig | None = None,
        retrieval_factory: Callable[[AsyncSession], RetrievalService] | None = None,
        sandbox_factory: SandboxFactory | None = None,
        mcp_tools_factory: McpToolsFactory | None = None,
        model_route_resolver: ModelRouteResolver | None = None,
        interactive: bool = True,
        suggestions_enabled: bool = _DEFAULT_SUGGESTIONS_ENABLED,
        suggestions_count: int = _DEFAULT_SUGGESTIONS_COUNT,
        suggestions_timeout_seconds: float = _DEFAULT_SUGGESTIONS_TIMEOUT_SECONDS,
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
        # The per-turn concurrent tool-call cap (#412, ADR-0016 §5) — bounds the
        # read-only batch's parallelism AND its per-answer session draw.
        self._tool_concurrency = tool_concurrency
        # The context-assembler budget knobs (ADR-0016 §1, issue #410): the
        # unknown-model fallback window + the output headroom. Injectable so the
        # API wires ``Settings``; defaults (module constants) keep offline tests
        # and any un-wired caller building prompts exactly as before.
        self._context_config = context_config or ContextConfig()
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
        # Whether a live user is on the other end of the stream (spec 0006
        # #429). Interactive (the chat API, the default): a valid ``ask_user``
        # call ends the turn as a clarifying question. Non-interactive (headless
        # runs, the assistant preview harness): interception is OFF — the call
        # falls through to the governed runner, whose handler refuses with
        # "proceed with your best interpretation", so a background run can never
        # end on a question nobody will answer.
        self._interactive = interactive
        # Follow-up suggestions knobs (spec 0006 #429): config-gated, bounded
        # count, hard timeout. The API wires ``Settings.chat_suggestions_*``.
        self._suggestions_enabled = suggestions_enabled
        self._suggestions_count = suggestions_count
        self._suggestions_timeout_seconds = suggestions_timeout_seconds

    async def run(
        self,
        *,
        stream_id: str,
        session_id: UUID,
        question: str,
        model: str,
        history: Sequence[ChatMessage],
        collection_ids: list[UUID] | None,
        document_ids: list[UUID] | None = None,
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
                    document_ids=document_ids,
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
                        # Provider cache accounting (#409, ADR-0016 §2.6) —
                        # additive contract fields; zero when the provider
                        # reports no cache detail.
                        "cachedPromptTokens": result.cached_prompt_tokens,
                        "cacheWriteTokens": result.cache_write_tokens,
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
        document_ids: list[UUID] | None = None,
        assistant_config: AssistantRunConfig | None = None,
        custom_instructions: str | None = None,
        simulate_writes: bool = False,
    ) -> _RunResult:
        """The tool-calling loop: search → ground → stream → persist."""
        tenant_id = self._principal.tenant_id
        retrieval = self._retrieval_factory(session)
        audit = AuditSink(AuditEventRepository(session, tenant_id))
        # Live run-phase progress (spec 0006 #429): transient ``event:step``
        # envelopes bracketing the phases. Nothing else streams while a model
        # turn is in flight (turns are buffered, #148), so these are what keeps
        # the pane honest between ``start`` and the first delta.
        await self._emit_step(state, key="prepare", label="Preparing", step_state="started")
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

        # Assemble the prompt under the model's input budget (ADR-0016 §1, #410),
        # replacing the old inline ``[system, *history, question]`` build and the
        # count-based history slice that used to live in ``chat_service``. History
        # trims oldest-first to fit; an oversize *fixed* prompt raises a typed
        # ``context_too_large`` (422) that ``run``'s ``except AppError`` arm turns
        # into the terminal problem envelope — a deliberate refusal, never a
        # provider 422. The tokenizer keys off the RESOLVED route model (the raw
        # id the provider actually sees). The derived ``retrieval_k`` flows into
        # the ``ToolContext`` below so a search issued after assembly respects the
        # same window.
        # Pinned documents (spec 0007 #429): the model-visible question carries a
        # short note so the model knows retrieval is scoped and searches rather
        # than answering unaided. Only the ASSEMBLED prompt sees it — the
        # persisted user message, the audit query hash, and the suggestions
        # prompt all use the raw question. Deliberately count-only: resolving
        # names here would need a permission-checked lookup surface this feature
        # doesn't otherwise require (the user already sees the names as pills).
        question_for_model = question
        if document_ids:
            question_for_model = (
                f"{question}\n\n[The user attached {len(document_ids)} specific "
                "document(s) to this message; document searches for this answer "
                "are scoped to them. Search them before answering.]"
            )
        assembled = assemble_context(
            model=route.model,
            system_prompt=system_prompt,
            history=history,
            question=question_for_model,
            tools=advertised,
            config=self._context_config,
        )
        messages: list[ChatMessage] = assembled.messages
        await self._emit_step(state, key="prepare", label="Preparing", step_state="completed")

        # Citations keyed by chunk_id so the same passage cited across turns is
        # recorded once (INV-3 set), preserving first-seen order.
        cited: dict[UUID, GroundedCitation] = {}
        # Which tool CALL carried which passages — (chunk_id, RENDERED snippet)
        # pairs in passage order (#415). The rendered snippet (trimmed to the
        # run's snippet budget, exactly as ``_render_passages`` showed it) is what
        # the model actually saw, so it is the "verbatim" unit the compactor must
        # preserve: uncited results digest first, and when a cited result must
        # compact as the last resort its digest re-embeds these snippets verbatim
        # (ADR-0016 §3.1) — the evidence behind an existing citation never leaves
        # the model's view.
        result_passage_snippets: dict[str, tuple[tuple[UUID, str], ...]] = {}
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
        # Set iff a turn ended with a valid ``ask_user`` call (spec 0006 #429):
        # the loop breaks and the turn persists as a clarifying question.
        # (Named ask_question: ``question`` is this method's user-question param.)
        ask_question: AskUserQuestion | None = None

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
            # Pinned documents (spec 0007 #429): passage search narrows to these
            # ids — an ADDITIONAL filter over the caller's allow-set, applied
            # inside retrieval/ (INV-2 unchanged: an id the caller cannot access
            # contributes nothing and discloses nothing).
            document_ids=document_ids,
            # The retrieval knobs the assembler derived from the input budget
            # (ADR-0016 §1 degrade order): the DEFAULT ``k`` (used when the model
            # omits it) plus the enforceable ceiling ``max_k`` that clamps even an
            # explicit model-supplied ``k`` (#424 review, finding 3) — both shrink
            # under a tight window so a search issued after assembly cannot
            # immediately overflow the context it was budgeted against. Set here,
            # before any tool runs (never shrunk retroactively).
            default_k=assembled.retrieval_k,
            max_k=assembled.max_k,
            snippet_budget=assembled.snippet_budget,
            session_id=session_id,
            sandbox=sandbox,
            # Read-only test/preview mode (F-AB-5, issue #215): a T1 file-writing tool
            # builds + validates but persists nothing, so a test run mutates no state.
            # ``run_python`` (T2) is already denied for a test run because the sandbox
            # seam is left unwired above (``sandbox=None``), so no container launches.
            simulate_writes=simulate_writes,
        )

        budget_exhausted = True
        for turn_index in range(max_tool_turns):
            # Re-fit the GROWN transcript to the input budget before every turn
            # (#424 review, finding 1): the loop appends each turn's tool-call +
            # tool-result messages, so a one-shot assembly at the top does not
            # keep later turns within the window. ``fit_transcript`` sheds oldest
            # conversation history (protecting the system head and this answer's
            # live tool tail); if even that cannot fit it raises the typed
            # ``context_too_large``, which ``run``'s ``except AppError`` arm turns
            # into the terminal problem envelope — never an over-budget call.
            messages = fit_transcript(
                messages,
                model=route.model,
                tools=advertised,
                config=self._context_config,
                cited_snippets=_cited_snippets_by_call(result_passage_snippets, cited),
            )
            await self._emit_step(
                state, key="think", label="Thinking", step_state="started", turn=turn_index + 1
            )
            turn_tool_calls, finish_reason, turn_text = await self._stream_one_turn(
                messages=messages, route=route, usage=usage, tools=advertised
            )
            if not turn_tool_calls:
                # Tool-free turn → this is the answer. Only now is its text known
                # to be answer content (not narration), so stream it now and stop.
                await self._emit_step(
                    state,
                    key="think",
                    label="Thinking",
                    step_state="completed",
                    turn=turn_index + 1,
                )
                await self._publish_text(state, turn_text)
                answer_chunks = turn_text
                budget_exhausted = False
                break

            # A valid ``ask_user`` ends the turn as a clarifying question (spec
            # 0006 §2): the FIRST valid call wins and NOTHING in this batch
            # executes (the in-run transcript is discarded at turn end, so no
            # dangling tool protocol). An ask_user with malformed arguments is
            # NOT selected here — it falls through to the governed runner below,
            # whose handler rejects it with a typed ``tool_bad_args`` result the
            # model reads and recovers from (#429 AC-N1). Non-interactive
            # consumers never intercept: the runner's handler refuses instead.
            # Interception also requires the tool to be in the run's ALLOW-LIST
            # (#434 review, finding 2): a hallucinated call from an assistant
            # that excluded ask_user reaches the runner and gets the ordinary
            # ``tool_not_permitted`` result — governance, not control flow.
            ask_question = (
                _select_ask_user(turn_tool_calls)
                if self._interactive and ASK_USER_TOOL_NAME in allowed
                else None
            )
            if ask_question is not None:
                finish_reason = "ask_user"
                budget_exhausted = False
                await self._emit_step(
                    state,
                    key="think",
                    label="Thinking",
                    step_state="completed",
                    detail="needs your input",
                    turn=turn_index + 1,
                )
                break

            await self._emit_step(
                state,
                key="think",
                label="Thinking",
                step_state="completed",
                detail=(
                    f"requested {len(turn_tool_calls)} tool"
                    f"{'' if len(turn_tool_calls) == 1 else 's'}"
                ),
                turn=turn_index + 1,
            )
            # The assistant turn that requested tools must be in the transcript
            # before its tool results (provider protocol). Its narration text is
            # dropped — neither streamed nor persisted.
            messages.append(
                ChatMessage(role=Role.ASSISTANT, content="", tool_calls=tuple(turn_tool_calls))
            )
            results = await self._run_tool_batch(
                state=state,
                runner=runner,
                audit=audit,
                context=tool_context,
                calls=turn_tool_calls,
                message_id=assistant_message_id,
            )
            for call, result in zip(turn_tool_calls, results, strict=True):
                total_hits += result.hit_count
                # Transcript messages append in ORIGINAL call order (#412) —
                # the provider protocol pairs each tool reply to its request,
                # and a deterministic order keeps the prompt prefix stable for
                # caching (ADR-0016 §2) regardless of completion order.
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        content=result.content,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                # Remember this call's passages as (chunk_id, rendered snippet)
                # so the compactor can protect/re-embed its cited evidence (#415).
                # The snippet is derived through the SAME renderer the tool reply
                # used (#431 NEW-1) — byte-identical to what the model saw,
                # ellipsis and all, so the two can never drift.
                if result.passages:
                    result_passage_snippets[call.id] = tuple(
                        (
                            p.chunk_id,
                            _retrieval_impl.rendered_snippet(p.text, assembled.snippet_budget),
                        )
                        for p in result.passages
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
            # live stream and the stored message agree. Re-fit here too (#424
            # finding 1): the forced-synthesis call sends the full accumulated
            # transcript, so it must respect the budget like every other turn.
            messages = fit_transcript(
                messages,
                model=route.model,
                tools=advertised,
                config=self._context_config,
                cited_snippets=_cited_snippets_by_call(result_passage_snippets, cited),
            )
            await self._emit_step(
                state,
                key="think",
                label="Thinking",
                step_state="started",
                detail="wrapping up",
                turn=max_tool_turns + 1,
            )
            _, finish_reason, turn_text = await self._stream_one_turn(
                messages=messages, route=route, usage=usage, tools=advertised, tool_choice="none"
            )
            await self._emit_step(
                state,
                key="think",
                label="Thinking",
                step_state="completed",
                turn=max_tool_turns + 1,
            )
            await self._publish_text(state, turn_text)
            answer_chunks = turn_text

        if ask_question is not None:
            # The clarifying-question turn (spec 0006 #429). The question text IS
            # the assistant message; it persists with the structured payload so
            # the options re-render after reload, and with ZERO citations — a
            # question makes no claims (INV-3), so passages retrieved before the
            # model chose to ask are deliberately not attached. No suggestions
            # (the options are the suggestions). The stream then ends
            # ``done(finishReason="ask_user")``.
            await self._persist(
                session=session,
                tenant_id=tenant_id,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                model=model,
                content=ask_question.question,
                citations=[],
                question=ask_question,
            )
            await LlmUsageRepository(session, tenant_id).record(
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                context_prompt_tokens=usage.last_turn_prompt_tokens,
                session_id=session_id,
                message_id=assistant_message_id,
            )
            await self._audit_answer(
                audit=audit,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                question=question,
                model=model,
                citation_count=0,
                retrieved_hits=total_hits,
                cited_document_ids=[],
            )
            await self._emit_ask_user(state, assistant_message_id, ask_question)
            return _RunResult(
                finish_reason="ask_user",
                citation_count=0,
                citations=(),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )

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

        await self._emit_step(state, key="finalize", label="Finalizing", step_state="started")
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
        await self._emit_step(state, key="finalize", label="Finalizing", step_state="completed")

        # Follow-up suggestions (spec 0006 #429): one config-gated, time-bounded
        # completion over the visible conversation tail — no retrieval, so no new
        # INV-2 surface. Any failure / parse miss ⇒ no event, never an error. Its
        # token usage folds into ``usage`` BEFORE the answer's single llm_usage
        # row records below, so the nicety's real cost is accounted (#409).
        # Skipped for the honest "couldn't find it" fallback (HAX guideline 10:
        # suppress suggestions on low-confidence/refusal answers — follow-ups to
        # a failed answer read as engagement bait).
        if self._suggestions_enabled and answer_text != NO_SOURCES_FALLBACK:
            await self._emit_step(
                state, key="suggest", label="Suggesting follow-ups", step_state="started"
            )
            suggestions = await self._generate_suggestions(
                question=question, answer=answer_text, route=route, usage=usage
            )
            await self._emit_step(
                state, key="suggest", label="Suggesting follow-ups", step_state="completed"
            )
            if suggestions:
                await self._publish(
                    state,
                    envelopes.event(
                        state.stream_id,
                        state.next_seq(),
                        name="suggestions",
                        data={
                            "messageId": str(assistant_message_id),
                            "suggestions": suggestions,
                        },
                    ),
                )

        # Record the answer's summed token/cache usage (#409) — one row per
        # answer, in the SAME transaction as the message it accounts for. Runs
        # AFTER suggestion generation so the row carries the whole answer's cost
        # (turn loop + the suggestions nicety, spec 0006 #429). ``model`` is the
        # requested id so a per-tenant ``provider:`` id stays attributable,
        # matching the message row. Zeroed fields (a provider that omitted
        # usage) still record: the answer happened, and "no usage reported"
        # must be visible, not a gap.
        await LlmUsageRepository(session, tenant_id).record(
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            context_prompt_tokens=usage.last_turn_prompt_tokens,
            session_id=session_id,
            message_id=assistant_message_id,
        )

        return _RunResult(
            finish_reason=finish_reason,
            citation_count=len(stored_citations),
            citations=tuple(stored_citations),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cache_write_tokens=usage.cache_write_tokens,
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
                usage.last_turn_prompt_tokens = ev.usage.prompt_tokens
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

    async def _run_tool_batch(
        self,
        *,
        state: _StreamState,
        runner: ToolRunner,
        audit: AuditSink,
        context: ToolContext,
        calls: Sequence[ToolCall],
        message_id: UUID,
    ) -> list[ToolResult]:
        """Run one turn's tool calls — read-only concurrently, the rest serially after.

        The concurrent executor (ADR-0016 §5, issue #412). The runner stays the
        single governance chokepoint for every call (allow-list → autonomy →
        approval → bounded execute → ordered audit + trace row, issue #207);
        this method owns only the *scheduling* around it:

        * The **fan-out set** is the static read-only calls plus unresolvable
          names (:meth:`ToolRunner.is_concurrency_safe`) — engaged only when it
          has 2+ members AND ``tool_concurrency`` > 1. Its ``tool_call`` events
          emit together at fan-out (dispatch, call order — the client sees the
          batch's plan immediately); its ordinals are claimed at that same
          moment, in call order. Each ``tool_result`` event emits when its
          call's ``run`` returns — which, because the runner's finalise drain
          persists in dispatch order, is also dispatch order: a result is
          never visible on the wire before its audit + trace row are written,
          even though the HANDLERS overlap freely and may complete in any
          order (the batch still takes ~max of the individual latencies).
        * Each fanned-out call that can actually reach handler execution
          (:meth:`ToolRunner.requires_call_scope`) gets an **isolated call
          scope**, entered by the runner only after governance passes: the
          batch semaphore slot, its own ``AsyncSession`` (tenant-bound via
          :func:`bind_tenant`, RLS-armed), its own ``RetrievalService``; the
          write seams (``artifacts``/``sandbox``) are stripped — a read-only
          tool has no business with them, and both are bound to the runtime
          session. The scope closes before the runner's ordered finalise, so a
          completed call holds neither its session nor its slot while waiting
          to persist. A denial-only call (unknown / off-list) fans out
          scopeless — a refusal never costs a pool connection.
        * Everything else — side-effecting (T1+) and ALL dynamic MCP tools (v1
          conservatism, ADR-0016 §5; their handlers close over runtime-session
          collaborators), plus every call when the fan-out set is too small or
          the cap is 1 — runs in the **serial form**, in call order, exactly
          the pre-#412 shape: ``tool_call`` at its own dispatch → govern +
          execute on the runtime context → ``retrieval.query`` audit →
          ``tool_result``. Ordinals claim at each run's entry, so the trace's
          dispatch order is the true execution order (fan-out first, then the
          serial tail).
        * For fanned-out retrieval calls the ``retrieval.query`` audit emits
          after the whole fan-out completes (call order): it rides the runtime
          session, and only then is that session guaranteed single-writer
          again. Under a mid-batch abort these audits are skipped along with
          the answer — the whole answer transaction rolls back atomically
          (pre-#412 semantics: a cancelled answer persists nothing).

        Every publish here happens in THIS coroutine — never in a worker — so
        the stream's ``seq`` mint + publish stay atomic and the wire order
        stays monotonic without a lock.
        """
        safe = [i for i, c in enumerate(calls) if runner.is_concurrency_safe(c.name)]
        fan_out = safe if self._tool_concurrency > 1 and len(safe) > 1 else []
        fanned = set(fan_out)
        serial_form = [i for i in range(len(calls)) if i not in fanned]
        done: dict[int, ToolResult] = {}

        if fan_out:
            for i in fan_out:
                await self._publish_tool_call(state, calls[i])
            ordinals = {i: runner.claim_ordinal() for i in fan_out}
            semaphore = asyncio.Semaphore(self._tool_concurrency)
            tenant_id = self._principal.tenant_id

            @asynccontextmanager
            async def _call_scope() -> AsyncIterator[ToolContext]:
                # The isolated call scope (ADR-0016 §5): the semaphore bounds
                # how many scopes — and so how many extra pool connections —
                # exist at once per answer; the session closes (rolling back
                # the read transaction) when the handler returns.
                async with semaphore, self._sessionmaker() as call_session:
                    await bind_tenant(call_session, tenant_id)
                    yield replace(
                        context,
                        retrieval=self._retrieval_factory(call_session),
                        artifacts=None,
                        sandbox=None,
                    )

            async def _worker(i: int) -> tuple[int, ToolResult]:
                call = calls[i]
                scope = _call_scope if runner.requires_call_scope(call.name) else None
                return i, await runner.run(
                    call=call,
                    context=context,
                    message_id=message_id,
                    ordinal=ordinals[i],
                    scope=scope,
                )

            tasks = [asyncio.create_task(_worker(i)) for i in fan_out]
            try:
                for fut in asyncio.as_completed(tasks):
                    i, result = await fut
                    done[i] = result
                    await self._publish_tool_result(state, calls[i], result)
            except BaseException:
                # A worker raised something the runner does not absorb
                # (cancellation, or an infrastructure fault like a failed
                # session open). Reap the siblings before propagating so no
                # orphaned task outlives the stream, then let ``run`` map the
                # raise to the terminal envelope exactly as the serial path
                # did. The answer transaction rolls back as a whole (nothing
                # half-persists), upholding the runner's claimed-ordinal
                # invariant — no finalise waiter survives the abort.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            # The fanned-out retrieval audits (call order), now that the
            # runtime session is single-writer again.
            for i in fan_out:
                if _is_retrieval_call(calls[i]):
                    await self._audit_retrieval(audit=audit, call=calls[i], result=done[i])

        # The serial form — the pre-#412 per-call shape, byte-for-byte:
        # dispatch event → govern + execute (runtime context, no extra
        # session) → retrieval audit → result event.
        for i in serial_form:
            call = calls[i]
            await self._publish_tool_call(state, call)
            result = await runner.run(call=call, context=context, message_id=message_id)
            done[i] = result
            if _is_retrieval_call(call):
                await self._audit_retrieval(audit=audit, call=call, result=result)
            await self._publish_tool_result(state, call, result)

        return [done[i] for i in range(len(calls))]

    async def _publish_tool_call(self, state: _StreamState, call: ToolCall) -> None:
        """Surface one requested call on the stream (the dispatch half of the trace)."""
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="tool_call",
                data={"callId": call.id, "tool": call.name, "args": call.arguments},
            ),
        )

    async def _publish_tool_result(
        self, state: _StreamState, call: ToolCall, result: ToolResult
    ) -> None:
        """Surface one completed call's outcome on the stream (correlated by ``callId``)."""
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
        question: AskUserQuestion | None = None,
    ) -> list[GroundedCitation]:
        """Persist the assistant message + its citations; return citations w/ ids.

        The message row uses the pre-minted ``assistant_message_id`` (the same id
        that rode ``start``) so the WS ``messageId`` and the stored row agree. Each
        citation persists with the **source** char span (deep-link to the document,
        CC-11) and reloads carrying its row id for the citation events.
        ``question`` is set only for a clarifying-question turn (spec 0006 #429),
        persisting the structured payload the UI re-renders options from.
        """
        message_repo = MessageRepository(session, tenant_id)
        citation_repo = CitationRepository(session, tenant_id)
        await message_repo.add_with_id(
            message_id=assistant_message_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=content,
            model=model,
            question=question,
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

    # --- spec 0006 (#429) helpers -------------------------------------------

    async def _emit_step(
        self,
        state: _StreamState,
        *,
        key: str,
        label: str,
        step_state: str,
        detail: str | None = None,
        turn: int | None = None,
    ) -> None:
        """Publish one ``event:step`` phase envelope (contract ChatStep).

        Transient run-visibility state (never persisted; replayed idempotently
        by ``seq`` like every envelope). ``turn`` disambiguates repeating keys
        (``think`` restarts per model turn).
        """
        data: dict[str, object] = {"key": key, "label": label, "state": step_state}
        if detail:
            data["detail"] = detail
        if turn is not None:
            data["turn"] = turn
        await self._publish(
            state,
            envelopes.event(state.stream_id, state.next_seq(), name="step", data=data),
        )

    async def _emit_ask_user(
        self, state: _StreamState, message_id: UUID, ask: AskUserQuestion
    ) -> None:
        """Publish the ``event:ask_user`` clarifying-question envelope (ChatAskUser)."""
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="ask_user",
                data={
                    "messageId": str(message_id),
                    "question": ask.question,
                    "options": [
                        {
                            "label": o.label,
                            **({"description": o.description} if o.description else {}),
                        }
                        for o in ask.options
                    ],
                    "allowFreeText": ask.allow_free_text,
                },
            ),
        )

    async def _generate_suggestions(
        self, *, question: str, answer: str, route: ModelRoute, usage: _Usage
    ) -> list[str]:
        """Propose follow-up questions for the settled answer, or ``[]``.

        One non-streamed completion on the answer's already-resolved route,
        hard-bounded by the configured timeout. Sees ONLY the visible
        conversation tail (bounded question head + answer tail) — never
        retrieved passages or tool output — so it cannot surface anything the
        caller wasn't already shown (spec 0006 §5). Every failure mode (gateway
        error, timeout, unparseable output) returns ``[]``: the nicety must
        never degrade the answer. Its token usage folds into ``usage`` so the
        answer's llm_usage row accounts for it (#409).
        """
        request = render_follow_up_request(
            question=question[:_SUGGESTIONS_QUESTION_HEAD_CHARS],
            answer=answer[-_SUGGESTIONS_ANSWER_TAIL_CHARS:],
            count=self._suggestions_count,
        )
        prompt = [
            ChatMessage(role=Role.SYSTEM, content=FOLLOW_UP_SYSTEM_PROMPT),
            ChatMessage(role=Role.USER, content=request),
        ]
        try:
            completion = await asyncio.wait_for(
                self._gateway.chat(
                    prompt,
                    model=route.model,
                    api_key=route.api_key,
                    api_base=route.api_base,
                    max_tokens=_SUGGESTIONS_MAX_TOKENS,
                ),
                timeout=self._suggestions_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — a nicety must never break the stream
            # Type only — the message may carry vendor detail (same discipline
            # as the run()-level handler). CancelledError is a BaseException in
            # 3.12, so shutdown cancellation still propagates.
            log.debug("chat_runtime.suggestions_failed", error_type=type(exc).__name__)
            return []
        usage.add(completion.usage)
        return _parse_suggestions(completion.content, limit=self._suggestions_count)

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
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0


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


def _select_ask_user(calls: Sequence[ToolCall]) -> AskUserQuestion | None:
    """The FIRST valid ``ask_user`` call of a turn's batch, parsed — or ``None``.

    Spec 0006 §2: a valid clarifying question ends the turn and nothing else in
    the batch executes. A malformed ``ask_user`` is NOT selected — it stays in
    the batch for the governed runner, whose handler rejects it with a typed
    ``tool_bad_args`` result the model recovers from (#429 AC-N1).
    """
    for call in calls:
        if call.name != ASK_USER_TOOL_NAME:
            continue
        try:
            return AskUserQuestion.parse(call.arguments)
        except AskUserValidationError:
            continue
    return None


def _parse_suggestions(text: str, *, limit: int) -> list[str]:
    """Parse the follow-up completion into clean suggestion strings (or ``[]``).

    Strict-ish by design (spec 0006 §2 — unparseable ⇒ silent skip): accepts the
    instructed JSON array (optionally fenced or embedded in prose — the bracket
    slice) but does NOT scrape free text, which would risk rendering prose as
    chips. Items are trimmed, deduped case-insensitively, length-capped, and
    bounded to ``limit``.
    """
    raw = text.strip()
    if raw.startswith("```"):
        # Drop a ```json fence: cut the fence lines, keep the body.
        first_newline = raw.find("\n")
        raw = raw[first_newline + 1 :] if first_newline != -1 else ""
        stripped = raw.rstrip()
        if stripped.endswith("```"):
            raw = stripped[:-3]
    candidates: list[object] = []
    for attempt in (raw, _bracket_slice(raw)):
        if not attempt:
            continue
        try:
            parsed = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            # Tolerate the common object envelope ({"follow_ups": [...]} — the
            # Open-WebUI-style contract) alongside the instructed bare array.
            for key in ("follow_ups", "suggestions", "questions"):
                value = parsed.get(key)
                if isinstance(value, list):
                    parsed = value
                    break
        if isinstance(parsed, list):
            candidates = parsed
            break
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, dict):
            # Tolerate [{"question": "..."}] — the intent is unambiguous.
            item = item.get("question") or item.get("text") or ""
        suggestion = str(item).strip()[:_SUGGESTION_MAX_CHARS]
        key = suggestion.casefold()
        if not suggestion or key in seen:
            continue
        seen.add(key)
        out.append(suggestion)
        if len(out) >= limit:
            break
    return out


def _bracket_slice(raw: str) -> str | None:
    """The outermost ``[...]`` slice of ``raw``, or ``None`` (JSON-in-prose rescue)."""
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return None
    return raw[start : end + 1]


def _hash_query(query: str) -> str:
    """A stable, non-reversible hash of the query (audit metadata, spec 0004 §2.4).

    The audit log records a query **hash**, not the raw query, so the trail does
    not duplicate potentially sensitive question text while still letting a
    reviewer correlate retrieval + answer events for the same turn.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _cited_snippets_by_call(
    result_passage_snippets: dict[str, tuple[tuple[UUID, str], ...]],
    cited: dict[UUID, GroundedCitation],
) -> dict[str, tuple[str, ...]]:
    """Map each tool ``call.id`` to the RENDERED snippets the answer cited from it (#415).

    Handed to :func:`~app.llm.context.fit_transcript` as ``cited_snippets``: the
    compactor digests uncited results first, and when a cited result must compact
    as the last resort, its digest re-embeds these snippets **verbatim**
    (ADR-0016 §3.1) — so the evidence behind an existing citation never leaves
    the model's view. The snippet is the *rendered* form (trimmed to the run's
    snippet budget — exactly what the transcript showed the model), deduped by
    chunk in passage order (deterministic); chunks the answer did not cite
    contribute nothing.
    """
    if not cited:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for call_id, pairs in result_passage_snippets.items():
        seen: set[UUID] = set()
        snippets: list[str] = []
        for chunk_id, snippet in pairs:
            if chunk_id in cited and chunk_id not in seen:
                seen.add(chunk_id)
                snippets.append(snippet)
        if snippets:
            out[call_id] = tuple(snippets)
    return out


def _is_retrieval_call(call: ToolCall) -> bool:
    """Whether a tool call targets a retrieval tool (gets the ``retrieval.query`` audit).

    The retrieval tools additionally emit the retrieval-semantics audit event
    (query hash + document ids + hit count, spec 0004 §2.4) on top of the generic
    ``tool.*`` events the runner emits for every tool. Keyed off the retrieval impl's
    declared names (``_RETRIEVAL_TOOL_NAMES``, read from ``TOOLS``) so a newly added
    retrieval tool is covered automatically and a non-retrieval tool never is.
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
