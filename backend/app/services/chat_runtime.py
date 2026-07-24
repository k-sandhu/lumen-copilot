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
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.core.errors import AppError, DependencyError
from app.core.logging import get_logger
from app.db.repositories import (
    AuditEventRepository,
    CitationRepository,
    LlmUsageRepository,
    MessageRepository,
    SessionSummaryRepository,
    TenantRepository,
    ToolInvocationRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.chat import AskUserQuestion, AskUserValidationError, GroundedCitation
from app.domain.entities import AuditOutcome, AutonomyLevel, MessageRole
from app.domain.llm import ChatMessage, Role, TokenUsage, ToolCall, ToolSpec
from app.domain.tools import ToolResult
from app.llm import LLMGateway, LlmProviderError
from app.llm.context import (
    ContextConfig,
    TokenCounter,
    assemble_context,
    fit_transcript,
    litellm_token_counter,
    memoizing_token_counter,
)
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
from app.services.provider_models import (
    ModelRoute,
    ModelRouteResolver,
    is_provider_model_id,
)
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
    can create + link the ``code_run`` (using its own short transactions plus the
    parent chat ``session_id`` / assistant ``message_id``), stream over the answer's
    ``stream_id`` with the shared monotonic ``next_seq`` (so ``code_output`` /
    ``code_result`` interleave with the answer's envelopes), and carry the audit
    correlation. Everything is the runtime's — never model/tool input.
    """

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
# Turn-level retry policy (ADR-0016 §4, #413). Retries are bounded (≤2) with
# a short backoff, applied ONLY to gateway-classified RETRYABLE faults (timeout,
# connection, 429, provider 5xx) and ONLY to buffered turns — nothing of a
# retried turn was ever published, so a retry can never duplicate wire output,
# and tools run BETWEEN turns, so a retry can never re-execute a tool. A 429's
# Retry-After hint stretches the backoff but is capped: a hostile/lazy header
# must not stall the answer.
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 2.0)
_RETRY_AFTER_CAP_SECONDS = 10.0
# Interactive per-turn deadline (#489). ``None`` (the module default, and every
# non-interactive / offline caller) = no runtime-level turn deadline: the gateway's
# ``llm_timeout_seconds`` request budget governs, unchanged. When the chat API wires
# ``Settings.llm_interactive_timeout_seconds`` a live answer caps each resilient
# turn (all same-route retries + backoffs + one failover attempt) at that budget
# via an ``asyncio.timeout``, so a hung/unreachable provider surfaces one typed 503
# within the budget instead of stacking the batch timeout per retry.
_DEFAULT_TURN_TIMEOUT_SECONDS: float | None = None
# Interactive attempt cap (#489). ``None`` = the full same-route retry ladder
# (``1 + len(_RETRY_BACKOFF_SECONDS)`` = 3 attempts). The chat API wires 2 (one
# retry) so a live turn does not stack the ladder inside the interactive budget.
_DEFAULT_INTERACTIVE_MAX_ATTEMPTS: int | None = None
# Answer/synthesis output ceiling (#488). ``None`` (the module default, and every
# offline/headless caller) = unbounded, the exact pre-#488 wire, so scripted turns
# keep their shapes. The chat API wires ``Settings.chat_answer_max_tokens`` so a
# live answer's length — and the tail of the streaming wait — is bounded.
_DEFAULT_ANSWER_MAX_TOKENS: int | None = None


@dataclass(slots=True)
class _RouteState:
    """The answer's LIVE model route + remaining failover candidates (#413).

    Mutable on purpose: when a route exhausts its retry budget the resilient
    turn wrapper advances ``model``/``route`` to the next fallback and the rest
    of the answer stays there (no per-turn thrashing). ``model`` is always the
    id that is ACTUALLY answering — the value ``done``, the message row, the
    usage row, and the audit record must carry (ADR-0016 §4).
    """

    model: str
    route: ModelRoute
    fallbacks: list[str] = field(default_factory=list)
    # Closed usage scopes (#413 / ADR-0016 §2.6): one (model, frozen usage)
    # per route that spent tokens and was then failed away from. Each gets its
    # own message-less ``llm_usage`` row — spend is attributed to the model
    # that actually incurred it, never re-labelled under the winner.
    spent: list[tuple[str, _Usage]] = field(default_factory=list)


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
# The dedicated suggestions model (#490). Empty ⇒ inherit the answer route (the
# pre-#490 behaviour and the default for headless / offline callers that never
# configure one). The chat API wires ``Settings.chat_suggestions_model`` (a
# FAST-tier id) so the nicety never rides the answer's frontier model.
_DEFAULT_SUGGESTIONS_MODEL = ""
_SUGGESTIONS_ANSWER_TAIL_CHARS = 4000
_SUGGESTIONS_QUESTION_HEAD_CHARS = 1000
_SUGGESTIONS_MAX_TOKENS = 400
_SUGGESTION_MAX_CHARS = 200

# Streamed-text coalescing defaults (issue #487) — mirrored from
# ``Settings.chat_text_coalesce_chars`` / ``_seconds``, which the API wires in.
# Module constants so an un-wired caller (offline tests, the headless runtimes)
# still coalesces exactly like production.
_DEFAULT_TEXT_COALESCE_CHARS = 160
_DEFAULT_TEXT_COALESCE_SECONDS = 0.04


@dataclass(frozen=True, slots=True)
class _TextKind:
    """What a buffered run of streamed text will be published as.

    Two runs coalesce only when their kind is equal, so answer text can never
    merge into a narration envelope (or vice versa) and narration of different
    turns stays separate — the #148/#414 separation is structural, not a
    convention the flush policy has to remember.
    """

    envelope: str  # "delta" (answer text) | "narration" (transient turn status)
    turn: int | None = None


_ANSWER_TEXT = _TextKind("delta")


@dataclass(slots=True)
class _TextCoalescer:
    """Flush policy for streamed text envelopes (issue #487).

    Buffers adjacent chunks of one :class:`_TextKind` and hands back the
    coalesced string when the policy fires — whichever comes FIRST, the
    character budget or the elapsed-time deadline. The caller must also
    :meth:`take` unconditionally before minting any other envelope, which is
    what keeps ``seq`` monotonic on the wire (a buffered chunk must never be
    published after an envelope minted later).

    ``clock`` is a **monotonic** source (``time.monotonic`` in production),
    injected so tests are deterministic without sleeping.
    """

    max_chars: int
    max_delay_seconds: float
    clock: Callable[[], float]
    kind: _TextKind | None = None
    _parts: list[str] = field(default_factory=list)
    _chars: int = 0
    _opened_at: float = 0.0

    def add(self, kind: _TextKind, chunk: str) -> str | None:
        """Buffer ``chunk``; return coalesced text when the policy says flush.

        The caller must have drained a buffer of a different ``kind`` first
        (see :meth:`take`) — mixing kinds in one envelope is not representable.
        """
        assert self.kind is None or self.kind == kind, "flush before switching kind"
        now = self.clock()
        if self.kind is None:
            self.kind = kind
            self._opened_at = now
        self._parts.append(chunk)
        self._chars += len(chunk)
        if self._chars >= self.max_chars or (now - self._opened_at) >= self.max_delay_seconds:
            return self.take()
        return None

    def take(self) -> str | None:
        """Drain the buffer unconditionally (``None`` when empty)."""
        if not self._parts:
            self.kind = None
            return None
        text = "".join(self._parts)
        self._parts.clear()
        self._chars = 0
        self.kind = None
        return text


@dataclass(slots=True)
class _StreamState:
    """Mutable per-stream bookkeeping (seq counter + accumulated answer)."""

    stream_id: str
    # The #487 text-coalescing buffer. Per stream because it holds unpublished
    # wire content: whatever is in it MUST be flushed before the terminal.
    buffer: _TextCoalescer
    seq: int = 0
    # Set once a terminal (``done``/``error``) has been published, so the
    # exactly-one-terminal contract holds even when two terminal paths race —
    # e.g. an error already emitted and then a shutdown ``CancelledError`` tries
    # to emit its own (issue #156).
    terminal_sent: bool = False
    # True once at least one answer ``delta`` has actually been FLUSHED to the
    # wire for the current speculative turn (#488). Set in ``_publish_text_
    # envelope`` (delta kind), cleared by ``_retract_answer``. It is what tells a
    # retraction whether there is anything on the client to discard: buffered-but-
    # never-flushed speculative text is dropped silently (no ``answer_retract``),
    # while flushed text earns the retraction envelope.
    answer_on_wire: bool = False

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

    def snapshot_and_reset(self) -> _Usage:
        """Freeze the current scope's totals and zero the accumulator (#413).

        Called at a model failover: the tokens spent so far belong to the ROUTE
        that spent them (ADR-0016 §2.6 — one usage row per model attempt
        scope), so the closing scope is snapshotted for its own ``llm_usage``
        row and the shared accumulator restarts at zero for the new route. The
        object identity is preserved — every holder of the accumulator keeps
        counting into the new scope.
        """
        frozen = replace(self)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cached_prompt_tokens = 0
        self.cache_write_tokens = 0
        self.last_turn_prompt_tokens = None
        return frozen


def _answer_usage_totals(route_state: _RouteState, current: _Usage) -> _Usage:
    """The ANSWER-total usage across every route scope (#413).

    The ``done`` envelope reports what the whole answer cost (billing view);
    the per-scope ``llm_usage`` rows carry the per-model attribution. The
    window-occupancy field stays the WINNING scope's — a dead route's last
    prompt size is meaningless for the context meter.
    """
    total = replace(current)
    for _model, scope in route_state.spent:
        total.prompt_tokens += scope.prompt_tokens
        total.completion_tokens += scope.completion_tokens
        total.total_tokens += scope.total_tokens
        total.cached_prompt_tokens += scope.cached_prompt_tokens
        total.cache_write_tokens += scope.cache_write_tokens
    return total


def _log_cache_kpi(result: _RunResult) -> None:
    """Emit the per-answer cache-hit KPI (#411 / ADR-0016 §2.6) — exactly once
    per SUCCESSFUL answer, on every success terminal (``ask_user`` included).
    Called from ``run()`` AFTER the commit and the ``done`` publication — a
    commit failure takes the error arm, and a failed ``done`` publication
    propagates out of ``run()``; either way control never reaches the KPI
    site, so a failed answer can never appear in the series (its spend is
    the salvage ledger's job). A route that reported no usage still emits, with
    ``usage_reported=false`` and a null ratio — the series must be a
    denominator over all successful answers, never a selection that silently
    drops the routes operators most need to see. Counts only; no prompt text,
    ids, or secrets.
    """
    reported = bool(result.prompt_tokens)
    log.info(
        "llm.cache_kpi",
        cached_prompt_tokens=result.cached_prompt_tokens,
        prompt_tokens=result.prompt_tokens,
        cache_hit_ratio=(
            round(result.cached_prompt_tokens / result.prompt_tokens, 3) if reported else None
        ),
        cache_write_tokens=result.cache_write_tokens,
        usage_reported=reported,
    )


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
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
        fallback_model_validator: Callable[[AsyncSession, str], Awaitable[bool]] | None = None,
        retrieval_factory: Callable[[AsyncSession], RetrievalService] | None = None,
        sandbox_factory: SandboxFactory | None = None,
        mcp_tools_factory: McpToolsFactory | None = None,
        model_route_resolver: ModelRouteResolver | None = None,
        interactive: bool = True,
        suggestions_enabled: bool = _DEFAULT_SUGGESTIONS_ENABLED,
        suggestions_count: int = _DEFAULT_SUGGESTIONS_COUNT,
        suggestions_timeout_seconds: float = _DEFAULT_SUGGESTIONS_TIMEOUT_SECONDS,
        suggestions_model: str = _DEFAULT_SUGGESTIONS_MODEL,
        text_coalesce_chars: int = _DEFAULT_TEXT_COALESCE_CHARS,
        text_coalesce_seconds: float = _DEFAULT_TEXT_COALESCE_SECONDS,
        clock: Callable[[], float] | None = None,
        turn_timeout_seconds: float | None = _DEFAULT_TURN_TIMEOUT_SECONDS,
        interactive_max_attempts: int | None = _DEFAULT_INTERACTIVE_MAX_ATTEMPTS,
        answer_max_tokens: int | None = _DEFAULT_ANSWER_MAX_TOKENS,
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
        # The #413 retry backoff sleeper — injectable so tests observe/skip the
        # waits; production uses asyncio.sleep.
        self._retry_sleep: Callable[[float], Awaitable[None]] = retry_sleep or asyncio.sleep
        # Failure-path usage salvage (#440 round-2 NEW-2): set once the answer
        # has a route state + accumulator; read by ``run``'s error arms to
        # persist spent scopes in an INDEPENDENT transaction when the answer
        # transaction rolls back (error/cancel) — providers billed those calls.
        self._salvage: tuple[_RouteState, _Usage] | None = None
        # The prompt-cache steering key (#411): the answer's session id, set
        # per answer in _answer (the runtime is constructed per answer).
        self._cache_key: UUID | None = None
        # The #413 fallback revalidator: the SAME allow-list check the send path
        # applies (config registry / the tenant's enabled providers), re-run at
        # answer start so stored config that went stale is dropped fail-closed.
        # ``None`` (offline tests / config-only runtimes) skips revalidation —
        # the structural filter + the provider-route guard below still apply.
        self._fallback_validator = fallback_model_validator
        # The context-assembler budget knobs (ADR-0016 §1, issue #410): the
        # unknown-model fallback window + the output headroom. Injectable so the
        # API wires ``Settings``; defaults (module constants) keep offline tests
        # and any un-wired caller building prompts exactly as before.
        self._context_config = context_config or ContextConfig()
        # Per-answer, per-model memoizing token counters (#491, AC-3). The answer
        # loop refits the grown transcript before every model turn; without
        # memoisation each refit re-tokenises the WHOLE transcript, so an answer
        # is O(turns x transcript) tokeniser work on the event loop. One memoiser
        # per model (keyed on the wire text) makes each refit O(new messages) —
        # the system prompt, tools block, question, and prior turns are cache
        # hits. Per-model because a failover refit deliberately re-counts under
        # the fallback model's tokeniser. The runtime is constructed per answer,
        # so the cache is bounded by (and lives only for) one answer.
        self._token_counters: dict[str, TokenCounter] = {}
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
        # The dedicated suggestions model (#490): resolved to its own route so
        # the nicety runs on a FAST model, not the answer's (possibly frontier)
        # route. Empty ⇒ inherit the answer route (pre-#490 behaviour).
        self._suggestions_model = suggestions_model
        # Streamed-text coalescing knobs (#487): the character budget and the
        # elapsed-time deadline, whichever fires first. ``clock`` is the
        # MONOTONIC time source, injectable so tests are deterministic without
        # sleeping (never a wall clock — a step backwards would stall a flush).
        self._text_coalesce_chars = text_coalesce_chars
        self._text_coalesce_seconds = text_coalesce_seconds
        self._clock: Callable[[], float] = clock or time.monotonic
        # Interactive timeout budget (#489). ``turn_timeout_seconds`` caps a whole
        # resilient turn (all same-route retries + backoffs + one failover) as an
        # asyncio deadline — the load-bearing bound for a hung provider; ``None``
        # leaves the batch behaviour (the gateway request timeout only).
        # ``interactive_max_attempts`` caps the same-route retry ladder for a live
        # turn (2 = one retry); ``None`` keeps the full 3-attempt ladder for
        # batch/headless callers. Neither is set by the headless/preview/offline
        # constructors, so their behaviour is byte-identical to pre-#489.
        self._turn_timeout_seconds = turn_timeout_seconds
        self._interactive_max_attempts = interactive_max_attempts
        # Answer/synthesis output ceiling (#488), threaded into every answer-turn
        # ``stream_tools`` call. ``None`` ⇒ unbounded (the pre-#488 wire); the chat
        # API wires ``Settings.chat_answer_max_tokens``. A 0 from config is treated
        # as unbounded (the setting's documented kill switch).
        self._answer_max_tokens = answer_max_tokens or None

    def _token_counter_for(self, model: str) -> TokenCounter:
        """The memoizing token counter for ``model`` (#491) — one per answer/model.

        Threaded into every ``assemble_context`` / ``fit_transcript`` call so the
        expensive tokeniser fires only for the messages each turn appends, not the
        whole grown transcript every turn. Cached per model id: the primary route
        accumulates hits across its turns, and a failover route gets its own
        memoiser (its tokeniser and window differ, so its counts must be fresh).
        """
        memo = self._token_counters.get(model)
        if memo is None:
            memo = memoizing_token_counter(litellm_token_counter(model))
            self._token_counters[model] = memo
        return memo

    def _new_coalescer(self) -> _TextCoalescer:
        """The per-stream text buffer, built from this runtime's flush policy."""
        return _TextCoalescer(
            max_chars=self._text_coalesce_chars,
            max_delay_seconds=self._text_coalesce_seconds,
            clock=self._clock,
        )

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
        summary: str | None = None,
        evidence: Sequence[tuple[UUID, UUID]] = (),
        mentioned_documents: Sequence[tuple[UUID, str]] = (),
    ) -> bool:
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
        state = _StreamState(stream_id=stream_id, buffer=self._new_coalescer())
        assistant_message_id = uuid.uuid4()
        await self._publish(
            state,
            envelopes.start(
                stream_id,
                await self._next_seq(state),
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
                    summary=summary,
                    evidence=evidence,
                    mentioned_documents=mentioned_documents,
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
            await self._salvage_usage_after_failure(session_id, assistant_message_id)
            await self._terminal_error(
                state,
                503,
                "Service Unavailable",
                "dependency_unavailable",
                "Server is shutting down.",
            )
            raise
        except AppError as exc:
            await self._salvage_usage_after_failure(session_id, assistant_message_id)
            await self._terminal_error(state, exc.status, exc.title, exc.code, exc.detail)
            return False
        except Exception as exc:  # noqa: BLE001 — never leak a vendor error to the client
            # Log the error *type* only (never the message — it may carry vendor
            # details / a key). The client gets an opaque 500 problem envelope.
            log.error("chat_runtime.failed", stream_id=stream_id, error_type=type(exc).__name__)
            await self._salvage_usage_after_failure(session_id, assistant_message_id)
            await self._terminal_error(state, 500, "Internal Server Error", "internal_error", None)
            return False

        # The terminal ``done`` is now published INSIDE ``_answer`` (#489), BEFORE
        # follow-up suggestions are generated, so the UI settles the instant the
        # answer is complete instead of waiting on the suggestions nicety. It rides
        # the still-open answer transaction: the message/citations/evidence are
        # flushed before it, and the only DB work left after it is the usage rows +
        # this commit. A commit failure after a published ``done`` is the documented
        # rollback-after-terminal exposure (deltas + citations already went on the
        # wire pre-commit too); the ``terminal_sent`` guard keeps it to exactly one
        # terminal and this KPI stays gated on a SUCCESSFUL commit below.
        # The KPI emits ONLY here — after the commit above and after ``_answer``
        # published its terminal ``done`` — so a failed answer (including a commit
        # failure, which lands in the ``except`` arms above) never appears in the
        # per-successful-answer series (round-2 review NEW-1; #489 keeps the gate).
        _log_cache_kpi(result)
        return True

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
        summary: str | None = None,
        evidence: Sequence[tuple[UUID, UUID]] = (),
        mentioned_documents: Sequence[tuple[UUID, str]] = (),
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
        # Resolve the chosen model's gateway route ONCE for the whole answer (PR 2a),
        # for a per-tenant ``provider:`` id this loads the provider + decrypts its key
        # a single time and yields the raw model id + api_key/base_url; for a config
        # id it is a plain passthrough with default credentials. The SAME resolved
        # route (and decrypted key) is reused across every gateway call of the turn
        # loop below — the key is never re-decrypted per call, nor logged.
        # The route lives in a mutable ``_RouteState`` (#413): when a model
        # exhausts its bounded retry budget on transient provider faults, the
        # resilient turn wrapper fails over down the tenant's configured
        # fallback list and the REST of the answer runs on the new route (no
        # per-turn thrashing back). ``route_state.model`` is therefore the
        # model that ACTUALLY produced the answer — what ``done``, the message
        # row, the usage row, and the audit record.
        # The per-session prompt-cache key (#411) the turn streamer forwards.
        self._cache_key = session_id
        route_state = _RouteState(
            model=model, route=await self._resolve_model_route(session, model)
        )
        # The per-run allow-list (issue #207 §2): for ad-hoc chat this is the
        # default read-only retrieval tools; a session started from an assistant
        # (E1) narrows/selects it from the registry (ADR-0011 §2). The governed
        # runner is the single chokepoint every tool call passes through — it
        # enforces the allow-list + approval seam, bounds each call, records a
        # ``tool_invocations`` row, and emits ``tool.invoked``/``tool.result``
        # (CC-7 / INV-6). Off-list / failing tools become results, not crashes.
        allowed = assistant_config.allowed if assistant_config is not None else default_allowlist()
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
        # One tenant fetch for both per-tenant knobs: the tool-turn budget
        # (issue #148) and the #413 fallback-model chain. The fallbacks are
        # defensively filtered (the admin write validates, but the column is
        # data): strings only, non-empty, minus the primary itself.
        tenant_row = await TenantRepository(session).get(tenant_id)
        override = tenant_row.max_tool_turns if tenant_row is not None else None
        max_tool_turns = max(1, override if override is not None else self._default_max_tool_turns)
        stored = tenant_row.fallback_models or [] if tenant_row else []
        structural: list[str] = []
        for m in stored:
            if isinstance(m, str) and m.strip() and m != model and m not in structural:
                structural.append(m)
        # The admin write bounds the chain at 3 (INV-8) — enforce it here too so
        # a raw-repository/seed writer cannot multiply worst-case retries.
        structural = structural[:3]
        if self._fallback_validator is not None:
            # Revalidate against the CURRENT tenant state (#440 review, finding
            # 4): a provider disabled/deleted since the admin write — or an id
            # smuggled past the service — is dropped here, fail-closed, before
            # any route resolution.
            validated: list[str] = []
            for m in structural:
                if await self._fallback_validator(session, m):
                    validated.append(m)
            route_state.fallbacks = validated
        else:
            route_state.fallbacks = structural

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
        # Evidence carry-forward rehydration (#416, ADR-0016 §3.2): the stored
        # digest is IDs ONLY; ONE bounded, permission-filtered metadata query
        # (#446 finding 4) re-checks every id against the requester's CURRENT
        # permissions — a revoked/deleted document is silently stripped (INV-2:
        # "the user saw it last turn" proves nothing about now). The SAME query
        # also covers the summary's mentioned-document names so a no-longer-
        # permitted name is REDACTED from the summary text before it can reach
        # the prompt (#446 finding 1 — revocation-safe memory).
        evidence_lines: list[str] = []
        by_doc: dict[UUID, list[UUID]] = {}
        for doc_id, chunk_id in evidence:
            by_doc.setdefault(doc_id, []).append(chunk_id)
        mention_ids = [doc_id for doc_id, _name in mentioned_documents]
        if by_doc or mention_ids:
            permitted = await retrieval.permitted_document_names(
                principal=self._principal,
                document_ids=[*by_doc.keys(), *mention_ids],
            )
            # Chunk-pair membership (#446 round-2, finding 4): only chunk ids
            # that genuinely belong to their claimed (permitted) document
            # survive — a corrupt digest cannot inject arbitrary ids.
            all_chunks = [c for chunks in by_doc.values() for c in chunks]
            chunk_owner = (
                await retrieval.valid_chunk_pairs(principal=self._principal, chunk_ids=all_chunks)
                if all_chunks
                else {}
            )
            for doc_id, chunk_ids in by_doc.items():
                name = permitted.get(doc_id)
                if name is None:
                    continue  # revoked/deleted — stripped without comment
                valid_chunks = [c for c in chunk_ids if chunk_owner.get(c) == doc_id]
                if not valid_chunks:
                    continue
                chunks_note = ", ".join(str(c) for c in valid_chunks)
                evidence_lines.append(
                    f"{name} (document_id {doc_id}; cited chunk(s): {chunks_note})"
                )
            if summary:
                for doc_id, name in mentioned_documents:
                    if doc_id not in permitted and name and name in summary:
                        summary = summary.replace(name, "[document no longer accessible]")
            # INV-6: rehydration is an audited read-side event — how many ids
            # were requested vs still permitted (never the content).
            await audit.emit(
                action=AuditAction.EVIDENCE_REHYDRATED,
                actor=AuditActor.user(self._principal.user_id),
                resource_type="session",
                resource_id=str(session_id),
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={
                    # The UNION of evidence + summary-mention checks — the
                    # mention-only case must not audit as zero (#446 r2 nit).
                    "requested_documents": len({*by_doc.keys(), *mention_ids}),
                    "permitted_documents": len(permitted),
                },
            )

        assembled = assemble_context(
            model=route_state.route.model,
            system_prompt=system_prompt,
            history=history,
            question=question_for_model,
            tools=advertised,
            config=self._context_config,
            counter=self._token_counter_for(route_state.route.model),
            summary=summary,
            evidence_lines=tuple(evidence_lines),
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
        # text is pre-tool narration ("I'll search…"), not answer content — it
        # streams as transient ``event:narration`` (#414), never as a ``delta``
        # and never persisted (issue #148). Streaming it as answer
        # would diverge the live answer from the stored message and re-expose the
        # narration-as-answer bug for clients that render the stream.
        answer_chunks: list[str] = []
        usage = _Usage()
        # Arm the failure-path salvage (#440 NEW-2): from here on, an error or
        # cancellation that rolls the answer transaction back still persists
        # any spent route scopes via ``run``'s error arms.
        self._salvage = (route_state, usage)
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

        def _refit(model_id: str) -> list[ChatMessage]:
            # Per-route prompt preparation (#440 review, finding 3): a failover
            # mid-turn refits the CURRENT transcript to the NEW route's
            # tokenizer/window before the first attempt on it — a smaller
            # fallback gets a prompt trimmed for its budget. Closes over the
            # live ``messages`` binding.
            return fit_transcript(
                messages,
                model=model_id,
                tools=advertised,
                config=self._context_config,
                counter=self._token_counter_for(model_id),
                cited_snippets=_cited_snippets_by_call(result_passage_snippets, cited),
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
                model=route_state.route.model,
                tools=advertised,
                config=self._context_config,
                counter=self._token_counter_for(route_state.route.model),
                cited_snippets=_cited_snippets_by_call(result_passage_snippets, cited),
            )
            await self._emit_step(
                state, key="think", label="Thinking", step_state="started", turn=turn_index + 1
            )

            async def _publish_narration(text: str, *, _turn: int = turn_index + 1) -> None:
                # ADR-0016 §6 (#414): a tool-calling turn's visible narration,
                # streamed live as transient status — NEVER a delta, never
                # persisted (the #148 invariant is untouched). Runs in the
                # answer coroutine, so seq mint + publish stay atomic.
                # Coalesced per turn (#487); the buffer is drained before the
                # turn's `think/completed` step, hence before its tool events.
                await self._stream_text(state, _TextKind("narration", _turn), text)

            turn_tool_calls, finish_reason, turn_text = await self._stream_turn_resilient(
                state=state,
                session=session,
                route_state=route_state,
                messages=messages,
                usage=usage,
                tools=advertised,
                refit=_refit,
                narrate=_publish_narration,
                max_tokens=self._answer_max_tokens,
            )
            if not turn_tool_calls:
                # Tool-free turn → this is the answer. Its text ALREADY streamed
                # live as answer deltas (#488, speculatively); no tool fragment
                # ever appeared, so none of it was retracted — it stays the
                # answer. Just close the think step (which flushes any tail of the
                # coalesced answer) and stop.
                await self._emit_step(
                    state,
                    key="think",
                    label="Thinking",
                    step_state="completed",
                    turn=turn_index + 1,
                )
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
            # before its tool results (provider protocol). Its narration text
            # already streamed live as transient ``event:narration`` (#414) —
            # it is dropped from the TRANSCRIPT/persistence, never from the
            # wire (the #148 answer invariant concerns deltas only).
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
            # narration from the exhausted tool turns streamed only as transient
            # ``event:narration`` (#414) — never as deltas — so the
            # live stream and the stored message agree. Re-fit here too (#424
            # finding 1): the forced-synthesis call sends the full accumulated
            # transcript, so it must respect the budget like every other turn.
            messages = fit_transcript(
                messages,
                model=route_state.route.model,
                tools=advertised,
                config=self._context_config,
                counter=self._token_counter_for(route_state.route.model),
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
            _, finish_reason, turn_text = await self._stream_turn_resilient(
                state=state,
                session=session,
                route_state=route_state,
                messages=messages,
                usage=usage,
                tools=advertised,
                tool_choice="none",
                refit=_refit,
                max_tokens=self._answer_max_tokens,
            )
            await self._emit_step(
                state,
                key="think",
                label="Thinking",
                step_state="completed",
                turn=max_tool_turns + 1,
            )
            # The forced synthesis is tool-IMPOSSIBLE (``tool_choice="none"``), so
            # its text streamed live unconditionally (#488, AC-2); the think step
            # above flushed any coalesced tail. Nothing to re-publish here.
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
                model=route_state.model,
                content=ask_question.question,
                citations=[],
                question=ask_question,
            )
            await self._record_usage_scopes(
                session=session,
                tenant_id=tenant_id,
                route_state=route_state,
                usage=usage,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
            )
            await self._audit_answer(
                audit=audit,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                question=question,
                model=route_state.model,
                citation_count=0,
                retrieved_hits=total_hits,
                cited_document_ids=[],
            )
            await self._emit_ask_user(state, assistant_message_id, ask_question)
            answer_usage = _answer_usage_totals(route_state, usage)
            ask_result = _RunResult(
                finish_reason="ask_user",
                model_used=route_state.model,
                citation_count=0,
                citations=(),
                prompt_tokens=answer_usage.prompt_tokens,
                completion_tokens=answer_usage.completion_tokens,
                total_tokens=answer_usage.total_tokens,
                cached_prompt_tokens=answer_usage.cached_prompt_tokens,
                cache_write_tokens=answer_usage.cache_write_tokens,
            )
            # The clarifying question IS the terminal (spec 0006): no suggestions
            # follow it, so ``done`` stops the stream at once (no pending window).
            await self._publish_terminal_done(
                state,
                assistant_message_id=assistant_message_id,
                result=ask_result,
                pending_suggestions=False,
            )
            return ask_result

        if finish_reason == "length":
            # Length continuation (ADR-0016 §4, #413): the answer hit the output
            # cap. Append the buffered partial as the assistant turn and run
            # exactly ONE continuation with tools disabled — a continuation can
            # never re-issue tool calls, so duplicate tool execution is
            # structurally impossible. The partial is already on the wire; the
            # continuation streams after it, so the live answer and the stored
            # message are both the concatenation. A continuation that is itself
            # length-capped is accepted as-is (ONE continuation, no loops) and
            # ``done`` honestly reports ``finishReason="length"``.
            if answer_chunks:
                # A zero-visible-text length turn (budget burned on non-text
                # content) skips the append — an empty assistant turn helps no
                # provider — but STILL gets its continuation (#440 finding 6).
                messages.append(ChatMessage(role=Role.ASSISTANT, content="".join(answer_chunks)))
            messages = fit_transcript(
                messages,
                model=route_state.route.model,
                tools=advertised,
                config=self._context_config,
                counter=self._token_counter_for(route_state.route.model),
                cited_snippets=_cited_snippets_by_call(result_passage_snippets, cited),
            )
            try:
                _, finish_reason, extra_chunks = await self._stream_turn_resilient(
                    state=state,
                    session=session,
                    route_state=route_state,
                    messages=messages,
                    usage=usage,
                    tools=advertised,
                    tool_choice="none",
                    refit=_refit,
                    # No failover mid-continuation (#440 round-2 NEW-5): the
                    # partial is already visible from THIS route; a different
                    # model finishing the sentence would falsify every
                    # single-model attribution surface (done.model, the
                    # message row, the audit).
                    allow_failover=False,
                    # The continuation runs AFTER a finalised answer block, so it
                    # must NOT stream live (#488): a whole-turn ``answer_retract``
                    # here would discard the already-finalised partial too. Buffer
                    # it and publish once it completes, the pre-#488 shape — which
                    # keeps "at most one un-finalised speculative block" true.
                    stream_answer=False,
                    max_tokens=self._answer_max_tokens,
                )
            except AppError as exc:
                # Best-effort continuation: if it cannot run (retries
                # exhausted, or a terminal fault), the already-published
                # partial IS the answer — honestly finish_reason="length" —
                # rather than erroring a stream that showed real content.
                # Nothing was published by the failed attempts (buffered
                # turns), so wire == stored still holds.
                log.warning("llm.length_continuation_failed", code=exc.code)
                extra_chunks = []
            else:
                await self._publish_text(state, extra_chunks)
            answer_chunks = [*answer_chunks, *extra_chunks]

        answer_text = "".join(answer_chunks).strip()

        # Persist the assistant message and its citations (INV-3): the citations
        # are exactly the permitted passages the tools returned — never more.
        if not answer_text:
            # Still no answer text — even a forced synthesis said nothing (e.g.
            # retrieval found nothing). Fall back to an honest "couldn't find it"
            # rather than persisting an empty turn.
            answer_text = NO_SOURCES_FALLBACK
            seq = await self._next_seq(state)
            await self._publish(
                state,
                envelopes.delta(state.stream_id, seq, {"text": answer_text}),
            )

        await self._emit_step(state, key="finalize", label="Finalizing", step_state="started")
        stored_citations = await self._persist(
            session=session,
            tenant_id=tenant_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            model=route_state.model,
            content=answer_text,
            citations=list(cited.values()),
        )
        # Evidence carry-forward WRITE (#416): the NEXT answer's digest is this
        # answer's cited ids — IDs only (ADR-0016 §3.2), replaced wholesale (a
        # zero-citation answer clears it). Rides the answer transaction; the
        # rolling summary TEXT updates asynchronously (the Celery task).
        await SessionSummaryRepository(session, tenant_id).upsert_evidence(
            session_id,
            evidence=[(c.document_id, c.chunk_id) for c in cited.values()],
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
            model=route_state.model,
            citation_count=len(stored_citations),
            retrieved_hits=total_hits,
            # Distinct cited documents, first-appearance order — the provenance
            # the Audit "Answers cited" KPI reads (#249).
            cited_document_ids=list(dict.fromkeys(str(c.document_id) for c in stored_citations)),
        )
        await self._emit_step(state, key="finalize", label="Finalizing", step_state="completed")

        # Suggestions are OFF the critical path now (#489). Whether they will be
        # ATTEMPTED is decided here — config-gated, and suppressed for the honest
        # "couldn't find it" fallback (HAX guideline 10 / AC-5: follow-ups to a
        # refusal read as engagement bait). The decision is declared to the client
        # on the terminal (``pendingSuggestions``) so the consumer knows to hold a
        # bounded grace open for the one post-terminal event.
        will_suggest = self._suggestions_enabled and answer_text != NO_SOURCES_FALLBACK

        # The answer's summed usage as reported on the terminal — the ANSWER's cost
        # (turn loop + any failover scopes), computed BEFORE the suggestions nicety
        # runs. ``done.usage`` is the answer terminal, so it must not wait on (nor
        # include) the post-terminal nicety; the suggestions tokens are still
        # accounted in the DB row below (#409 / AC-3).
        answer_usage = _answer_usage_totals(route_state, usage)
        run_result = _RunResult(
            finish_reason=finish_reason,
            model_used=route_state.model,
            citation_count=len(stored_citations),
            citations=tuple(stored_citations),
            prompt_tokens=answer_usage.prompt_tokens,
            completion_tokens=answer_usage.completion_tokens,
            total_tokens=answer_usage.total_tokens,
            cached_prompt_tokens=answer_usage.cached_prompt_tokens,
            cache_write_tokens=answer_usage.cache_write_tokens,
        )

        # Publish the terminal ``done`` FIRST (#489 AC-1) — the UI settles the
        # instant the answer is complete. It rides the still-open transaction; the
        # message/citations/evidence are already flushed, and the only DB work left
        # is the usage rows + the commit in ``run``.
        await self._publish_terminal_done(
            state,
            assistant_message_id=assistant_message_id,
            result=run_result,
            pending_suggestions=will_suggest,
        )

        # Follow-up suggestions (spec 0006 #429): one config-gated, time-bounded
        # completion over the visible conversation tail — no retrieval, so no new
        # INV-2 surface. Now generated AFTER the terminal and delivered as a bounded
        # POST-terminal ``event:suggestions`` (#489). Any failure / timeout / parse
        # miss ⇒ no event, never touching the already-published terminal (AC-6). Its
        # token usage folds into ``usage`` BEFORE the answer's single llm_usage row
        # records below, so the nicety's real cost is still accounted (#409 / AC-3).
        if will_suggest:
            suggestions_route = await self._resolve_suggestions_route(session, route_state.route)
            suggestions = await self._generate_suggestions(
                question=question, answer=answer_text, route=suggestions_route, usage=usage
            )
            if suggestions:
                await self._publish(
                    state,
                    envelopes.event(
                        state.stream_id,
                        await self._next_seq(state),
                        name="suggestions",
                        data={
                            "messageId": str(assistant_message_id),
                            "suggestions": suggestions,
                        },
                    ),
                )

        # Record the answer's summed token/cache usage (#409) — one row per
        # answer, in the SAME transaction as the message it accounts for. Still runs
        # AFTER suggestion generation so the row carries the whole answer's cost
        # (turn loop + the suggestions nicety), even though ``done.usage`` above
        # reported the answer-only total. ``model`` is the requested id so a
        # per-tenant ``provider:`` id stays attributable, matching the message row.
        # Zeroed fields (a provider that omitted usage) still record: the answer
        # happened, and "no usage reported" must be visible, not a gap.
        await self._record_usage_scopes(
            session=session,
            tenant_id=tenant_id,
            route_state=route_state,
            usage=usage,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
        )

        return run_result

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

        The seam borrows the raw ``state.next_seq`` (it publishes straight to the
        backplane, and a flush needs an ``await``). Safe because ``run_python``
        executes strictly BETWEEN model turns, while the #487 text buffer can
        only hold content published from the answer turn onwards — the buffer is
        provably empty whenever the seam runs.
        """
        if self._sandbox_factory is None or RUN_PYTHON_TOOL_NAME not in allowed:
            return None
        return self._sandbox_factory(
            SandboxContext(
                stream_id=state.stream_id,
                session_id=session_id,
                message_id=assistant_message_id,
                next_seq=state.next_seq,
            )
        )

    async def _salvage_usage_after_failure(
        self, session_id: UUID, assistant_message_id: UUID
    ) -> None:
        """Persist spent route scopes after an errored/cancelled answer (#413).

        The answer transaction rolled back — but the provider still billed
        every completed call. Best-effort, in a FRESH independent transaction:
        each scope that actually spent tokens records as a message-less row
        (there is no message) under its own model, grouped by ``answer_id``.
        Empty scopes are skipped (a 422 refused before any provider call must
        not pollute the ledger with zero rows). Never raises — a salvage
        failure is logged (type only) and the original fault stays the story.
        """
        if self._salvage is None:
            return
        route_state, usage = self._salvage
        scopes = [*route_state.spent, (route_state.model, usage)]
        spent = [(m, s) for m, s in scopes if s.total_tokens > 0 or s.prompt_tokens > 0]
        if not spent:
            return
        try:
            async with self._sessionmaker() as session:
                await bind_tenant(session, self._principal.tenant_id)
                repo = LlmUsageRepository(session, self._principal.tenant_id)
                for model, scope in spent:
                    await repo.record(
                        model=model,
                        prompt_tokens=scope.prompt_tokens,
                        completion_tokens=scope.completion_tokens,
                        total_tokens=scope.total_tokens,
                        cached_prompt_tokens=scope.cached_prompt_tokens,
                        cache_write_tokens=scope.cache_write_tokens,
                        context_prompt_tokens=scope.last_turn_prompt_tokens,
                        session_id=session_id,
                        answer_id=assistant_message_id,
                        message_id=None,
                    )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort; never mask the fault
            log.warning("llm.usage_salvage_failed", error_type=type(exc).__name__)

    async def _record_usage_scopes(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        route_state: _RouteState,
        usage: _Usage,
        session_id: UUID,
        assistant_message_id: UUID,
    ) -> None:
        """One ``llm_usage`` row per model route scope (#413, ADR-0016 §2.6).

        Spend is attributed to the model that INCURRED it: each failed-away
        route's frozen scope persists as a message-less row (the answer id
        would be wrong — that route produced no message), and only the winning
        scope attaches to the assistant message. Read-side totals aggregate
        rows, so billing/cache KPIs stay per-model-honest across failovers.
        Zero-usage closed scopes still record: "the route was tried and
        reported nothing" must be visible, not a gap (#409's posture).
        """
        repo = LlmUsageRepository(session, tenant_id)
        for spent_model, scope in route_state.spent:
            await repo.record(
                model=spent_model,
                prompt_tokens=scope.prompt_tokens,
                completion_tokens=scope.completion_tokens,
                total_tokens=scope.total_tokens,
                cached_prompt_tokens=scope.cached_prompt_tokens,
                cache_write_tokens=scope.cache_write_tokens,
                context_prompt_tokens=scope.last_turn_prompt_tokens,
                session_id=session_id,
                answer_id=assistant_message_id,
                message_id=None,
            )
        await repo.record(
            model=route_state.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            context_prompt_tokens=usage.last_turn_prompt_tokens,
            session_id=session_id,
            answer_id=assistant_message_id,
            message_id=assistant_message_id,
        )

    async def _stream_turn_resilient(
        self,
        *,
        state: _StreamState,
        session: AsyncSession,
        route_state: _RouteState,
        messages: list[ChatMessage],
        usage: _Usage,
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
        refit: Callable[[str], list[ChatMessage]] | None = None,
        allow_failover: bool = True,
        narrate: Callable[[str], Awaitable[None]] | None = None,
        stream_answer: bool = True,
        max_tokens: int | None = None,
    ) -> tuple[list[ToolCall], str, list[str]]:
        """One turn with bounded retries + model failover (ADR-0016 §4, #413/#488).

        Safe by construction even though the answer now streams LIVE (#488):
        speculative answer ``delta``s are wire output that can be UN-published, so
        a retried or failed-over turn is preceded by an ``answer_retract`` that
        undoes them — the retry window therefore stays open despite live streaming.
        Tools run between turns, so a retry can never re-execute a tool. A turn
        CLASSIFIED tool-calling (ADR-0016 §6, #414) publishes narration through the
        ``narrate`` seam — narration cannot be un-published, so from that
        classification instant the window is closed: no retry, no failover.

        ``stream_answer`` gates live answer streaming: the tool-loop and forced-
        synthesis turns stream (``True``); the length continuation buffers
        (``False``) because it runs after a finalised answer block, where a
        whole-turn retraction would wrongly discard the finalised partial too
        (#488). ``max_tokens`` bounds the turn's generation. Policy, in order:

        * a gateway-classified RETRYABLE fault (timeout / connection / 429 /
          provider 5xx) retries on the SAME route, ≤ ``len(_RETRY_BACKOFF_
          SECONDS)`` times with backoff (a 429's Retry-After hint stretches the
          wait, capped at ``_RETRY_AFTER_CAP_SECONDS``);
        * a TERMINAL fault (auth / permission / config — and anything the
          gateway could not classify) raises immediately, exactly as before
          #413: the typed error becomes the one terminal envelope, and no
          fallback is consulted (a fault a retry cannot fix on this route is a
          configuration problem to surface, not to paper over);
        * retries exhausted → fail over down the tenant's fallback list
          (:meth:`_advance_to_fallback`) and start the attempt cycle on the new
          route; the REST of the answer stays there. All candidates exhausted →
          the last retryable fault raises (one typed terminal, AC-3).

        A partially-consumed failed turn may already have folded provider
        ``usage`` into the running totals — deliberately kept: those tokens
        were genuinely spent, and the usage row is billing-honest.

        **Interactive deadline (#489).** When ``turn_timeout_seconds`` is set (the
        chat API path), the WHOLE resilient turn — every same-route retry, its
        backoffs, and any failover attempt — is bounded by one ``asyncio.timeout``.
        A hung/unreachable provider therefore surfaces a typed 503 within the budget
        (AC-4) instead of stacking the batch request timeout per retry. A fired
        deadline is re-raised as a typed :class:`DependencyError` (503) — never a raw
        ``TimeoutError``, which would bypass ``run``'s ``except AppError`` arm and
        become an opaque 500. ``None`` (batch/headless/offline) keeps the pre-#489
        behaviour: the gateway's request timeout is the only bound.
        """
        # ``narration`` closes the retry window (§6); ``answer`` records that
        # speculative answer text has been streamed live this attempt and is
        # therefore RETRACTABLE before a retry/failover (#488).
        emitted = {"narration": False, "answer": False}

        async def _speak_answer(text: str) -> None:
            # Speculative / tool-impossible answer text, streamed live as
            # ``delta`` (#488). Recorded BEFORE the publish so a fault mid-publish
            # still knows to retract on the way to a retry.
            emitted["answer"] = True
            await self._stream_text(state, _ANSWER_TEXT, text)

        async def _retract_speculative_answer() -> None:
            # Un-publish the current turn's speculative answer ``delta``s (#488):
            # discards the still-buffered tail and — if any delta already reached
            # the wire — emits ``answer_retract``. Resets the flag so the next
            # attempt streams cleanly and a second retract is a no-op.
            await self._retract_answer(state)
            emitted["answer"] = False

        async def _narrate_and_close_window(text: str) -> None:
            # ADR-0016 §4/§6 (#414): the window closes at the CLASSIFICATION
            # point — the first invocation of this callback — whether or not
            # any text was buffered yet (#447 round-2: a signal-only turn must
            # not retry either; the amendment closes the window at the
            # fragment, not at the first byte). Recorded BEFORE the publish so
            # a fault mid-publish still counts; empty text closes the window
            # without emitting a pointless envelope.
            emitted["narration"] = True
            if narrate is not None and text:
                await narrate(text)

        async def _run_attempts() -> tuple[list[ToolCall], str, list[str]]:
            nonlocal messages
            while True:
                last: LlmProviderError | None = None
                for attempt in range(self._turn_attempt_count()):
                    if attempt:
                        delay = _RETRY_BACKOFF_SECONDS[attempt - 1]
                        hint = last.retry_after_seconds if last is not None else None
                        if hint is not None:
                            delay = max(delay, min(hint, _RETRY_AFTER_CAP_SECONDS))
                        await self._retry_sleep(delay)
                    try:
                        return await self._stream_one_turn(
                            messages=messages,
                            route=route_state.route,
                            usage=usage,
                            tools=tools,
                            tool_choice=tool_choice,
                            max_tokens=max_tokens,
                            narrate=(_narrate_and_close_window if narrate is not None else None),
                            speak=(_speak_answer if stream_answer else None),
                            # Retraction is only meaningful where a tool call can
                            # still appear (narrate seam wired); a tool-impossible
                            # turn never classifies, so it never retracts here.
                            on_retract=(
                                _retract_speculative_answer if narrate is not None else None
                            ),
                        )
                    except LlmProviderError as exc:
                        if not exc.retryable or emitted["narration"]:
                            # Terminal — or the turn already emitted narration
                            # (ADR-0016 §4: the retry window closed at first
                            # emission; a retry could duplicate wire output).
                            raise
                        # Retryable and NOT classified tool-calling: any answer
                        # text streamed speculatively this attempt is retractable
                        # (#488). Un-publish it so the retry/failover re-streams
                        # cleanly — this is what keeps the window open despite
                        # live streaming.
                        if emitted["answer"]:
                            await _retract_speculative_answer()
                        last = exc
                        # Class-name-derived detail only — never vendor text (#36 AC-7).
                        log.warning(
                            "llm.turn_retryable_fault",
                            model=route_state.model,
                            attempt=attempt,
                            code=exc.code,
                        )
                # Retries exhausted on this route. Fail over if a candidate remains:
                # close the dying route's usage scope (its spend stays attributed to
                # it — ADR-0016 §2.6), then REFIT the transcript to the new route's
                # tokenizer/window (#440 review, finding 3): a smaller fallback must
                # get a prompt trimmed for ITS budget, not the primary's.
                if last is None:  # pragma: no cover — the loop always sets it
                    raise RuntimeError("retry loop exited without a fault")
                dying_model = route_state.model
                if not allow_failover:
                    # The length-continuation turn (#440 round-2 NEW-5): visible
                    # answer text is already on the wire from the CURRENT route, so
                    # switching models mid-answer would make ``done.model`` (and
                    # every attribution surface) name a model that produced only
                    # part of the visible content. Retries stay; failover does not.
                    raise last
                if not await self._advance_to_fallback(session, route_state):
                    raise last
                route_state.spent.append((dying_model, usage.snapshot_and_reset()))
                if refit is not None:
                    messages = refit(route_state.route.model)

        budget = self._turn_timeout_seconds
        if budget is None:
            return await _run_attempts()
        try:
            async with asyncio.timeout(budget) as cm:
                return await _run_attempts()
        except TimeoutError:
            if cm.expired():
                # The interactive per-turn budget expired on a hung/unreachable
                # provider (#489 AC-4). Surface it as the same typed 503 the
                # gateway maps a provider timeout to — the client gets one terminal
                # error within the budget, not the ~182s retry cliff.
                log.warning("llm.turn_timeout", model=route_state.model, budget=budget)
                raise DependencyError(
                    "The model did not respond within the interactive time budget."
                ) from None
            raise

    def _turn_attempt_count(self) -> int:
        """Same-route attempts for one turn — capped for the interactive path (#489).

        The full ladder is ``1 + len(_RETRY_BACKOFF_SECONDS)`` (3 attempts, the
        batch/headless default). The chat API caps it via ``interactive_max_attempts``
        (2 = one retry) so a live turn does not stack the ladder inside the
        interactive budget. Never exceeds the number of defined backoffs + 1.
        """
        attempts = 1 + len(_RETRY_BACKOFF_SECONDS)
        if self._interactive_max_attempts is not None:
            attempts = min(attempts, self._interactive_max_attempts)
        return attempts

    async def _advance_to_fallback(self, session: AsyncSession, route_state: _RouteState) -> bool:
        """Advance to the next resolvable fallback route; False when exhausted (#413).

        Candidates were validated at the admin write, but resolution can still
        fail live (a provider disabled since) — an unresolvable candidate is
        skipped (typed error class logged, never vendor text), the walk
        continues. On success the route state is REBOUND: the rest of the
        answer runs on the fallback, and ``route_state.model`` becomes the
        actually-used id the persistence layer records.
        """
        while route_state.fallbacks:
            candidate = route_state.fallbacks.pop(0)
            try:
                route = await self._resolve_model_route(session, candidate)
            except AppError as exc:
                log.warning("llm.fallback_unresolvable", model=candidate, code=exc.code)
                continue
            if is_provider_model_id(candidate) and route.model == candidate:
                # UNRESOLVED passthrough: a resolved ``provider:`` route always
                # rewrites ``model`` to the provider's raw id, so a route still
                # carrying the prefixed candidate means no resolver was wired
                # or the provider vanished. Sending it would hit the GLOBAL
                # gateway credentials with a bogus id — skip instead (#440
                # finding 4). A resolved ANONYMOUS provider (api_base set, no
                # key — a legitimate config) rewrites the model and passes
                # (#440 round-2 NEW-3).
                log.warning(
                    "llm.fallback_unresolvable", model=candidate, code="provider_unresolved"
                )
                continue
            log.warning("llm.failover", from_model=route_state.model, to_model=candidate)
            route_state.model = candidate
            route_state.route = route
            return True
        return False

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

    async def _resolve_suggestions_route(
        self, session: AsyncSession, answer_route: ModelRoute
    ) -> ModelRoute:
        """The follow-up-suggestions gateway route (#490).

        A DEDICATED model, resolved through the SAME per-tenant route seam as
        the answer, so the <=400-token nicety runs on a FAST id instead of the
        answer's (possibly frontier) route. An empty ``suggestions_model`` (the
        constructor default, and headless/offline callers) falls back to the
        answer route — the exact pre-#490 behaviour, so nothing regresses for
        callers that do not configure one.
        """
        if not self._suggestions_model:
            return answer_route
        return await self._resolve_model_route(session, self._suggestions_model)

    async def _stream_one_turn(
        self,
        *,
        messages: list[ChatMessage],
        route: ModelRoute,
        usage: _Usage,
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        narrate: Callable[[str], Awaitable[None]] | None = None,
        speak: Callable[[str], Awaitable[None]] | None = None,
        on_retract: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[list[ToolCall], str, list[str]]:
        """Consume one completion turn; return ``(tool_calls, finish_reason, text)``.

        Buffers the turn's text chunks (for persistence) and folds token ``usage``
        into the running total. Publishing is entirely through caller-supplied
        seams, so this method never decides what a chunk MEANS:

        * ``speak`` — when set (#488), each text chunk is forwarded LIVE as answer
          ``delta`` the instant it arrives, *before* the turn is proven tool-free.
          The caller enables it only where that is safe: a tool-impossible turn
          (``tool_choice="none"`` / empty tools) streams unconditionally, and a
          tool-advertised turn streams SPECULATIVELY.
        * ``narrate`` + ``on_retract`` — the classification seam (ADR-0016 §6,
          #414). The FIRST tool-call fragment proves the turn tool-calling, hence
          its text narration: ``on_retract`` un-publishes any speculative answer
          ``delta`` (#488), the buffer flushes through ``narrate`` as
          ``event:narration``, and the rest of the turn narrates live. ``narrate``
          also CLOSES the turn's retry window (§4 — an emitting turn never retries).

        Answer text is thus never published as answer once the turn is known
        tool-calling: it is retracted and re-emitted as narration, so the
        streamed-and-not-retracted answer still equals the persisted one (#148).
        ``tools`` is the run's allow-list rendered as ``ToolSpec``s (issue #207 §2).
        ``tool_choice="none"`` forces a tool-free turn — the final synthesis once
        the budget is spent. ``max_tokens`` bounds the generation (#488).

        ``route`` carries the answer's resolved gateway route (PR 2a): the raw model
        id + (for a per-tenant provider) the api_key/base_url override. The SAME
        route is passed on every turn of the loop, so a provider's decrypted key is
        reused, never re-decrypted per turn.
        """
        turn_tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        text_chunks: list[str] = []
        narrating = False
        async for ev in self._gateway.stream_tools(
            messages,
            tools=list(tools),
            model=route.model,
            tool_choice=tool_choice,
            api_key=route.api_key,
            api_base=route.api_base,
            # The prompt-cache steering key (#411): stable per session so
            # OpenAI-style implicit caching lands consecutive answers of one
            # conversation on the same cache shard.
            cache_key=str(self._cache_key) if self._cache_key else None,
            # Output ceiling (#488): only forwarded when set, so an unbounded
            # (offline/headless) turn keeps its exact pre-#488 wire.
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
        ):
            if (ev.tool_call_started or ev.tool_calls) and not narrating and narrate is not None:
                # The classification point (ADR-0016 §6, #414): the FIRST
                # tool-call fragment proves this turn is tool-calling, hence
                # its text is narration. First RETRACT any answer text already
                # streamed speculatively (#488) — the client discards it —
                # then flush the buffer as narration and stream the rest live;
                # ``narrate`` also CLOSES the turn's retry window (§4 — an
                # emitting turn never retries).
                narrating = True
                if on_retract is not None:
                    await on_retract()
                buffered = "".join(text_chunks)
                # ALWAYS invoke — an empty buffer still closes the retry
                # window (the amendment's letter: the window closes AT the
                # classification point, #447 round-2 blocker-1 edge); the
                # wrapper's callback skips publishing empty text.
                await narrate(buffered)
            if ev.text:
                if narrating and narrate is not None:
                    await narrate(ev.text)
                elif speak is not None:
                    # Speculative / tool-impossible answer text, streamed live
                    # as ``delta`` (#488). Only reached while NOT narrating.
                    await speak(ev.text)
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
        """Publish BUFFERED answer text as ``delta`` envelopes, coalesced (#487).

        Since #488 the answer/synthesis turns stream their text LIVE (through the
        ``speak`` seam), so this is now used ONLY by the length continuation — the
        one answer turn that stays buffered, because it runs after a finalised
        answer block where a whole-turn retraction could not apply (#488). Never
        for a tool-calling turn's pre-tool narration (issue #148), so the
        streamed-and-not-retracted answer equals the persisted one.

        Adjacent chunks are merged under the flush policy instead of minting one
        envelope per provider chunk. The CONCATENATION is byte-identical either
        way; only the envelope count changes. Anything still buffered is drained
        by the next :meth:`_next_seq` — i.e. before the ``finalize`` step, the
        citations, and the terminal.
        """
        for chunk in chunks:
            await self._stream_text(state, _ANSWER_TEXT, chunk)

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
                await self._next_seq(state),
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
                await self._next_seq(state),
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
            envelopes.event(state.stream_id, await self._next_seq(state), name="step", data=data),
        )

    async def _emit_ask_user(
        self, state: _StreamState, message_id: UUID, ask: AskUserQuestion
    ) -> None:
        """Publish the ``event:ask_user`` clarifying-question envelope (ChatAskUser)."""
        await self._publish(
            state,
            envelopes.event(
                state.stream_id,
                await self._next_seq(state),
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
                await self._next_seq(state),
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

    async def _publish_terminal_done(
        self,
        state: _StreamState,
        *,
        assistant_message_id: UUID,
        result: _RunResult,
        pending_suggestions: bool,
    ) -> None:
        """Publish the terminal ``done`` (the exactly-one terminal; #489).

        Moved out of ``run`` into ``_answer`` so the terminal settles the UI BEFORE
        the follow-up-suggestions nicety runs (AC-1). ``done.usage`` carries the
        ANSWER's summed usage (the suggestions call had not run yet); its cost is
        still recorded on the DB usage row afterwards (#409). ``pending_suggestions``
        declares that one post-terminal ``event:suggestions`` may still follow within
        the consumer's grace window; absent/false keeps the classic stop-at-terminal
        behaviour. The ``terminal_sent`` guard preserves exactly-one-terminal across
        the shutdown race (issue #156) exactly as ``run`` did before.

        The payload is byte-identical to the pre-#489 ``done`` (built in ``run``)
        except for the additive ``pendingSuggestions`` flag: the ACTUAL post-failover
        model, the citation count, and the answer usage sub-object.
        """
        if state.terminal_sent:
            # A terminal already fired (e.g. cancellation mid-answer raced ahead);
            # never publish a second terminal — exactly-one-terminal contract.
            return
        state.terminal_sent = True
        data: dict[str, object] = {
            "messageId": str(assistant_message_id),
            "finishReason": result.finish_reason,
            # The model that ACTUALLY answered (#413) — differs from ``start``'s
            # requested model after a failover.
            "model": result.model_used,
            "citationCount": result.citation_count,
            "usage": {
                "promptTokens": result.prompt_tokens,
                "completionTokens": result.completion_tokens,
                "totalTokens": result.total_tokens,
                # Provider cache accounting (#409, ADR-0016 §2.6) — additive
                # contract fields; zero when the provider reports no cache detail.
                "cachedPromptTokens": result.cached_prompt_tokens,
                "cacheWriteTokens": result.cache_write_tokens,
            },
        }
        if pending_suggestions:
            # One post-terminal ``event:suggestions`` may still follow (#489); the
            # subscriber holds a bounded grace open for it.
            data["pendingSuggestions"] = True
        await self._publish(
            state, envelopes.done(state.stream_id, await self._next_seq(state), data=data)
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
        seq = await self._next_seq(state)
        await self._publish(state, envelopes.error(state.stream_id, seq, problem))

    async def _next_seq(self, state: _StreamState) -> int:
        """Flush any buffered streamed text, then mint the next ``seq`` (#487).

        EVERY non-text envelope mints through here. Flushing *before* the mint is
        the whole ordering guarantee: a buffered ``delta``/narration always gets a
        lower ``seq`` **and** goes on the wire first, so coalescing can never
        strand text behind a terminal (subscribers stop at the terminal) nor
        publish an envelope whose ``seq`` was minted earlier than one already
        sent. The seq allocator itself stays the plain non-atomic counter — safe
        because every mint+publish pair runs in the answer coroutine.
        """
        await self._flush_text(state)
        return state.next_seq()

    async def _stream_text(self, state: _StreamState, kind: _TextKind, chunk: str) -> None:
        """Buffer one chunk of streamed text, publishing when the policy fires.

        Text of a different :class:`_TextKind` (answer vs narration, or a new
        turn's narration) drains the buffer first, so two kinds never merge into
        one envelope.
        """
        if not chunk:
            return
        if state.buffer.kind is not None and state.buffer.kind != kind:
            await self._flush_text(state)
        ready = state.buffer.add(kind, chunk)
        if ready is not None:
            await self._publish_text_envelope(state, kind, ready)

    async def _flush_text(self, state: _StreamState) -> None:
        """Publish whatever streamed text is buffered (no-op when empty)."""
        kind = state.buffer.kind
        text = state.buffer.take()
        if kind is None or text is None:
            return
        await self._publish_text_envelope(state, kind, text)

    async def _publish_text_envelope(self, state: _StreamState, kind: _TextKind, text: str) -> None:
        """Mint + publish one coalesced text envelope for ``kind``.

        Mints via ``state.next_seq()`` directly — going through
        :meth:`_next_seq` would recurse into the flush that produced ``text``.
        """
        if kind.envelope == "narration":
            envelope = envelopes.event(
                state.stream_id,
                state.next_seq(),
                name="narration",
                data={"text": text, "turn": kind.turn},
            )
        else:
            # An answer ``delta`` is now on the wire — a later retraction (#488)
            # has something to discard on the client.
            state.answer_on_wire = True
            envelope = envelopes.delta(state.stream_id, state.next_seq(), {"text": text})
        await self._publish(state, envelope)

    async def _retract_answer(self, state: _StreamState) -> None:
        """Retract the current speculative turn's answer ``delta``s (#488).

        First DISCARD any answer text still buffered in the coalescer — it is
        being retracted, so publishing it now (then discarding it on the client)
        would be pointless AND would race a stray ``delta`` onto the wire with a
        HIGHER ``seq`` than the retraction (a buffered chunk flushed by the next
        ``_next_seq`` would land after ``answer_retract`` and never be discarded).
        Then, only if a ``delta`` actually reached the wire, publish
        ``event:answer_retract`` — a whole-turn retraction (contract #488). A
        turn whose speculative text only ever buffered (never flushed) is undone
        by the discard alone, with no envelope. ``seq`` is minted directly (the
        buffer is already drained, so ``_next_seq``'s flush would be a no-op).
        """
        # The buffer, if non-empty here, holds this turn's un-flushed answer text
        # (kind == _ANSWER_TEXT). Discard it. Any other kind (impossible at a
        # retraction point) is flushed rather than silently dropped.
        if state.buffer.kind is None or state.buffer.kind == _ANSWER_TEXT:
            state.buffer.take()
        else:  # pragma: no cover — defensive; retraction only follows answer text
            await self._flush_text(state)
        if not state.answer_on_wire:
            return
        state.answer_on_wire = False
        seq = state.next_seq()
        await self._publish(state, envelopes.event(state.stream_id, seq, name="answer_retract"))

    async def _publish(self, state: _StreamState, envelope: dict[str, object]) -> None:
        await self._backplane.publish(state.stream_id, envelope)


@dataclass(frozen=True, slots=True)
class _RunResult:
    """Internal result of the answer loop (feeds the ``done`` envelope)."""

    finish_reason: str
    # The model that ACTUALLY produced the answer (#413): the requested id, or
    # the fallback the answer failed over to. Rides ``done`` so the client can
    # show/record what really answered.
    model_used: str
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
_RETRIEVAL_TOOL_NAMES: frozenset[str] = frozenset(defn.name for defn in _retrieval_impl.TOOLS)


__all__ = [
    "ChatRuntime",
    "McpToolsFactory",
    "ModelRoute",
    "ModelRouteResolver",
    "SandboxContext",
    "SandboxFactory",
]
