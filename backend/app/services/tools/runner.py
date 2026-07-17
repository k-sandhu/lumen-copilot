"""The governed tool runner (CC-7 #207) — the single tool-invocation chokepoint.

Every tool call in the system flows through :meth:`ToolRunner.run`, and there is
**no other path** to invoke a tool (AC-N). In one place it enforces the whole
governance contract, in this fixed order:

1. **Resolve** the tool in the registry — an unknown name → ``tool_not_found``.
2. **Allow-list** (issue #207 §2 / AC-2): a tool not in the per-run allowed set →
   ``tool_not_permitted``. Enforced here, once — no bypass.
3. **Autonomy gate** (issue #218 / ADR-0011 §3): a side-effecting **T1** tool is
   gated by the assistant's EFFECTIVE autonomy (already ``min``'d to the tenant cap
   by the run assembler). At ``suggest``/``draft`` a T1 write is refused
   (``autonomy_denied``) — the assistant may only suggest/draft, not act; at
   ``act_with_approval`` a T1 write is routed through the approval seam (step 4) like
   a higher tier; at ``act_auto`` it executes automatically (still audited). T0
   read-only tools are never autonomy-gated. Ad-hoc chat (no assistant) defaults to
   ``act_auto`` and its default set is all-T0, so this step is inert there.
4. **Approval seam** (issue #207 §3 / AC-3 / INV-7): a ``requires_approval`` tool
   (T2+) — and a T1 tool at ``act_with_approval`` (step 3) — is routed to the
   :class:`~app.services.tools.types.ApprovalGate` and blocks until approved; a
   denial → ``approval_denied`` and the handler never runs.
5. **Bounded execute** (issue #207 §7 / AC-5): the handler runs under a per-tool
   timeout; a raised or timed-out handler → an ``ok=False`` result with a safe
   message — never a crashed stream.
6. **Uniform result** (issue #207 §4): whatever happened, produce one
   :class:`~app.domain.tools.ToolResult`.
7. **Audit + trace** (issue #207 §4/§5 / AC-4 / INV-6): emit ``tool.invoked``
   (intent) and ``tool.result`` (outcome) through the one audit sink, and write a
   ``tool_invocations`` row — for **every** invocation, including a governance
   denial or a failure, so the trace has no silent gap.

Layering (ADR-0004): ``services/`` orchestration. It composes the registry, the
approval gate, the ``db/`` ``ToolInvocationRepository``, and the audit sink; it
returns a domain :class:`ToolResult` and leaks no adapter/vendor type upward.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from itertools import count
from uuid import UUID

from app.core.logging import get_logger
from app.db.repositories import ToolInvocationRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, AutonomyLevel
from app.domain.llm import ToolCall
from app.domain.tools import (
    ERROR_APPROVAL_DENIED,
    ERROR_AUTONOMY_DENIED,
    ERROR_NOT_FOUND,
    ERROR_NOT_PERMITTED,
    ERROR_TOOL_ERROR,
    ERROR_TOOL_TIMEOUT,
    RiskTier,
    ToolHandlerResult,
    ToolResult,
)
from app.services.audit import AuditSink
from app.services.tools.registry import UnknownToolError, get_tool
from app.services.tools.types import (
    ApprovalGate,
    ApprovalRequest,
    DenyAllApprovalGate,
    ToolContext,
    ToolDefinition,
)

log = get_logger(__name__)


# The isolated call-scope factory a concurrent dispatcher passes into
# :meth:`ToolRunner.run` (ADR-0016 §5, #412): each invocation yields the
# per-call :class:`ToolContext` the handler executes against — its own
# tenant-bound ``AsyncSession``-backed collaborators — and may bound the
# dispatcher's concurrency (the runtime's factory acquires the batch semaphore
# on enter). Entered only after governance passes; exited before the ordered
# finalise persists.
CallScopeFactory = Callable[[], AbstractAsyncContextManager[ToolContext]]


class _AutonomyDecision(enum.Enum):
    """How the run's effective autonomy resolves a side-effecting (T1) tool (#218).

    ``ALLOW`` = execute automatically (``act_auto``); ``APPROVE`` = route through the
    approval seam (``act_with_approval``); ``DENY`` = refuse — the assistant's level
    (``suggest``/``draft``) may not take a side effect at all.
    """

    ALLOW = "allow"
    APPROVE = "approve"
    DENY = "deny"


def _requires_autonomy_approval(definition: ToolDefinition) -> bool:
    """True for a side-effecting **T1** tool — the one autonomy gates at run time (#218).

    A T0 read-only tool is never autonomy-gated; a T2+ tool is already governed by the
    approval seam (``requires_approval``) regardless of autonomy, and T2+ stays out of
    MVP scope. So the autonomy gate concerns exactly the reversible-internal-write tier
    (``T1``, not ``requires_approval``): write_file, run_python's file effects.
    """
    return definition.risk_tier is RiskTier.T1 and not definition.requires_approval


def hash_args(arguments: dict[str, object]) -> str:
    """A stable, non-reversible hash of a tool call's arguments (spec 0004 §2.4).

    The ``tool_invocations`` row and the audit metadata store an argument **hash**,
    not the raw args — so the trace does not duplicate potentially sensitive
    argument text while a reviewer can still correlate identical calls. Keys are
    sorted so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` hash equal; non-JSON values
    fall back to ``repr`` so hashing never itself raises.
    """
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover — default=repr makes this unreachable
        canonical = repr(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolRunner:
    """Runs one tool call through the full governance path (the CC-7 gateway).

    Constructed per answer with the run's collaborators: the ``allowed`` tool set
    resolved from the session/assistant (issue #207 §2 — default = the three
    retrieval tools for ad-hoc chat), the tenant-scoped
    :class:`ToolInvocationRepository`, the :class:`AuditSink`, and the request
    correlation (``request_id``/``source_ip``) the audit events carry. The
    approval ``gate`` defaults to :class:`DenyAllApprovalGate` (inert for T0/T1;
    fail-closed for any gated tool until a real approval flow is wired).

    ``extra_tools`` is the per-run set of **dynamic** :class:`ToolDefinition`s that
    are not in the static ``impls/`` registry — namely the tenant's registered MCP
    tools (issue #227, namespaced ``mcp:<slug>:<tool>``). They are tenant-scoped and
    resolved fresh each run from :mod:`app.services.tools.mcp_bridge`, so they are
    passed in here rather than globally registered (a global static registration
    could leak an MCP server across tenants — INV-1). Resolution consults
    ``extra_tools`` first, then the static registry; the allow-list / approval /
    audit path (below) is then **identical** for a dynamic MCP tool and a native one
    — MCP adds no second pipeline (ADR-0012 §6).

    ``autonomy`` is the run's EFFECTIVE :class:`~app.domain.entities.AutonomyLevel`
    (issue #218) — the assistant's configured level already ``min``'d to the tenant
    admin cap by the run assembler. It gates side-effecting **T1** tools (below);
    read-only T0 tools are never affected. It defaults to ``ACT_AUTO`` so ad-hoc chat
    (no assistant) is unchanged — the ad-hoc default set is all-T0, so the gate is
    inert there.
    """

    def __init__(
        self,
        *,
        allowed: frozenset[str],
        invocations: ToolInvocationRepository,
        audit: AuditSink,
        actor: AuditActor,
        request_id: str,
        source_ip: str,
        session_id: UUID | None = None,
        gate: ApprovalGate | None = None,
        extra_tools: Mapping[str, ToolDefinition] | None = None,
        autonomy: AutonomyLevel = AutonomyLevel.ACT_AUTO,
    ) -> None:
        self._allowed = allowed
        self._invocations = invocations
        self._audit = audit
        self._actor = actor
        self._request_id = request_id
        self._source_ip = source_ip
        self._session_id = session_id
        self._gate: ApprovalGate = gate or DenyAllApprovalGate()
        self._extra_tools: Mapping[str, ToolDefinition] = extra_tools or {}
        self._autonomy = autonomy
        # Per-turn dispatch counter (#397, #412): a runner lives exactly one
        # answer turn, so this is the per-message ordinal the trace orders by.
        # Ordinals are claimed AT DISPATCH — the serial path claims at ``run``
        # entry; a concurrent dispatcher preassigns per call, in dispatch
        # order, via :meth:`claim_ordinal` before fanning out.
        self._ordinal = count()
        # The serialized persistence coordinator (ADR-0016 §5, #412):
        # ``_finalise`` writes (two audit events + the ``tool_invocations``
        # row) flush on the runner's one DB session, which admits no concurrent
        # operations — AND the ADR requires those writes to land in DISPATCH
        # order, not completion order. The condition gates each finalise until
        # every lower ordinal has persisted, so the physical write sequence of
        # all three durable record types follows dispatch even when handlers
        # complete out of order. Uncontended (zero overhead) on the serial
        # path. Invariant: every claimed ordinal reaches ``_finalise`` or the
        # whole batch aborts (the dispatcher cancels all siblings and the
        # answer terminates) — an abandoned ordinal never leaves a live waiter.
        self._persist_gate = asyncio.Condition()
        self._next_persist_ordinal = 0

    def claim_ordinal(self) -> int:
        """Preassign the next dispatch ordinal (ADR-0016 §5, #412).

        A concurrent dispatcher claims one ordinal per call, **in dispatch
        order, before fanning out** — so the trace orders by dispatch even when
        completions land out of order. Serial callers never need this:
        :meth:`run` claims its own at entry when none is passed. Synchronous on
        purpose — a mint can never interleave with another. Every claimed
        ordinal must then be passed to exactly one :meth:`run` call (the
        finalise drain persists strictly in ordinal order, so a claimed-but-
        never-run ordinal would stall its successors; the dispatcher's
        abort-the-batch error path upholds this).
        """
        return next(self._ordinal)

    def is_concurrency_safe(self, name: str) -> bool:
        """Whether ``name`` may join the turn's concurrent read-only batch (#412).

        True for a **static registry** read-only tool (structurally T0 and
        never approval-gated, per :class:`ToolDefinition`'s construction
        checks) and for an **unresolvable** name (its denial executes no
        handler, allocates no call scope — see :meth:`requires_call_scope` —
        and persists only through the ordered finalise drain). False for:

        * any side-effecting or gated tool — the v1 executor runs those
          serially *after* the read-only batch (ADR-0016 §5 conservatism), on
          the runtime's own context, where the approval gate's DB reads stay
          single-writer; and
        * ANY dynamic ``extra_tools`` (MCP) tool, read-only or not — an MCP
          handler is a closure over the per-run client whose auth resolver and
          ``secret.accessed`` audit are bound to the RUNTIME session, which a
          per-call scope cannot rebind. Fanning those out would race the
          shared session outside the persistence coordinator (and a raced
          secret read degrades to an anonymous outbound call). MCP fan-out
          needs per-call re-resolution (a full ADR-0016 §5 call-scope for the
          MCP seam) — deferred, tracked with the #427 amendments.
        """
        if name in self._extra_tools:
            return False
        definition = self._resolve(name)
        return definition is None or definition.read_only

    def requires_call_scope(self, name: str) -> bool:
        """Whether a concurrent call actually needs an isolated call scope (#412).

        Only a call that can reach **handler execution** needs one — a static,
        read-only tool that is on this run's allow-list. A denial-only call
        (unresolvable, or off the allow-list) never executes a handler and
        persists only through the coordinator, so opening a DB session for it
        would spend pool capacity on a refusal — under pool pressure that would
        turn a typed, audited denial into an infrastructure failure.
        """
        if name in self._extra_tools:
            return False
        definition = self._resolve(name)
        return definition is not None and definition.read_only and name in self._allowed

    async def run(
        self,
        *,
        call: ToolCall,
        context: ToolContext,
        message_id: UUID | None = None,
        ordinal: int | None = None,
        scope: CallScopeFactory | None = None,
    ) -> ToolResult:
        """Invoke ``call`` through the governed path; always return a :class:`ToolResult`.

        Never raises for a tool concern (an unknown/off-list/unapproved/failing
        tool is an ``ok=False`` result the model reads and the run recovers from).
        Emits ``tool.invoked`` before and ``tool.result`` after, and writes one
        ``tool_invocations`` row — for every outcome (AC-4/INV-6).

        ``ordinal`` is the call's trace dispatch ordinal (#412): ``None`` (the
        serial path, unchanged) claims the next one here at entry; a concurrent
        dispatcher preassigns them in dispatch order via :meth:`claim_ordinal`
        BEFORE fanning out, so the trace orders by dispatch even when
        completions land out of order (ADR-0016 §5).

        ``scope`` is the optional **isolated call-scope factory** (ADR-0016 §5,
        #412): an async context manager yielding the per-call
        :class:`ToolContext` a concurrent handler executes against (its own
        tenant-bound session + collaborators). Governance (resolve → allow-list
        → autonomy → approval) runs FIRST, on the base ``context``'s identity —
        a denial never opens the scope, so a refused call costs no session.
        The scope closes as soon as the handler returns, BEFORE the ordered
        finalise below — a completed call never holds its session (or the
        dispatcher's concurrency slot, which the factory may bound) while
        waiting its turn to persist.
        """
        if ordinal is None:
            ordinal = self.claim_ordinal()
        args_hash = hash_args(call.arguments)
        started = time.monotonic()

        # (1) Resolve. A dynamic per-run MCP tool (``extra_tools``, issue #227) is
        # tried first, then the static ``impls/`` registry. An unknown tool is denied
        # by default (deny-by-default) and never reaches the allow-list/handler.
        definition = self._resolve(call.name)

        # (2) Allow-list — the one enforcement point (AC-2 / AC-N). A tool absent
        # from the registry OR absent from this run's allowed set is refused with a
        # typed result; the handler is not invoked.
        if definition is None:
            return await self._finalise(
                call=call,
                args_hash=args_hash,
                message_id=message_id,
                ordinal=ordinal,
                result=ToolResult.failure(
                    call_id=call.id,
                    name=call.name,
                    error=ERROR_NOT_FOUND,
                    content=f"Unknown tool: {call.name}",
                    summary="unknown tool",
                    duration_ms=_elapsed_ms(started),
                ),
                outcome=AuditOutcome.DENIED,
            )
        if call.name not in self._allowed:
            return await self._finalise(
                call=call,
                args_hash=args_hash,
                message_id=message_id,
                ordinal=ordinal,
                result=ToolResult.failure(
                    call_id=call.id,
                    name=call.name,
                    error=ERROR_NOT_PERMITTED,
                    content=(
                        f"Tool {call.name!r} is not permitted for this session. "
                        "Use one of the available tools instead."
                    ),
                    summary="not permitted",
                    duration_ms=_elapsed_ms(started),
                ),
                outcome=AuditOutcome.DENIED,
            )

        # (3) Autonomy gate (issue #218 / ADR-0011 §3). A side-effecting **T1** tool
        # (write_file, run_python's file effects) is gated by the run's EFFECTIVE
        # autonomy — the assistant's level already min'd to the tenant admin cap.
        #   * suggest / draft (below act_with_approval): a T1 write is REFUSED here —
        #     the assistant may only suggest/draft, not execute a side effect
        #     (autonomy_denied), the handler never runs.
        #   * act_with_approval: fall through and route the T1 tool through the approval
        #     seam below (like a higher tier), so it executes only if approved.
        #   * act_auto: fall through and execute automatically (still audited).
        # T0 read-only tools are never autonomy-gated. This is deny-by-default: a
        # side-effecting tool needs an explicit autonomy level to run.
        if _requires_autonomy_approval(definition):
            decision = self._autonomy_decision(definition.risk_tier)
            if decision is _AutonomyDecision.DENY:
                return await self._finalise(
                    call=call,
                    args_hash=args_hash,
                    message_id=message_id,
                    ordinal=ordinal,
                    result=ToolResult.failure(
                        call_id=call.id,
                        name=call.name,
                        error=ERROR_AUTONOMY_DENIED,
                        content=(
                            f"Tool {call.name!r} takes an action this assistant's autonomy "
                            f"level ({self._autonomy.value!r}) does not permit. Raise the "
                            "assistant's autonomy to act on it."
                        ),
                        summary="autonomy denied",
                        duration_ms=_elapsed_ms(started),
                    ),
                    outcome=AuditOutcome.DENIED,
                )
            requires_gate = decision is _AutonomyDecision.APPROVE
        else:
            requires_gate = False

        # (4) Approval seam (AC-3 / INV-7). ``requires_approval`` (⇒ T2+) tools always
        # gate here; a T1 tool at ``act_with_approval`` also gates (``requires_gate``).
        # A denial refuses the call BEFORE the handler runs — no consequential action
        # executes without approval.
        if definition.requires_approval or requires_gate:
            approved = await self._gate.request(
                ApprovalRequest(
                    call_id=call.id,
                    tool_name=call.name,
                    risk_tier=definition.risk_tier,
                    principal=context.principal,
                    arguments=call.arguments,
                )
            )
            if not approved:
                return await self._finalise(
                    call=call,
                    args_hash=args_hash,
                    message_id=message_id,
                    ordinal=ordinal,
                    result=ToolResult.failure(
                        call_id=call.id,
                        name=call.name,
                        error=ERROR_APPROVAL_DENIED,
                        content=(
                            f"Tool {call.name!r} requires approval, which was not granted. "
                            "The action was not performed."
                        ),
                        summary="approval denied",
                        duration_ms=_elapsed_ms(started),
                    ),
                    outcome=AuditOutcome.DENIED,
                )

        # (5) Bounded execute (AC-5). A raised/timed-out handler becomes an
        # ok=False result — the stream never crashes. A concurrent call enters
        # its isolated scope ONLY here — after every governance gate passed —
        # and leaves it before the finalise drain (#412).
        if scope is None:
            result = await self._execute(definition, call, context, started)
        else:
            async with scope() as scoped_context:
                result = await self._execute(definition, call, scoped_context, started)
        outcome = AuditOutcome.ALLOWED if result.ok else AuditOutcome.ERROR
        return await self._finalise(
            call=call,
            args_hash=args_hash,
            message_id=message_id,
            ordinal=ordinal,
            result=result,
            outcome=outcome,
        )

    def _autonomy_decision(self, risk_tier: RiskTier) -> _AutonomyDecision:
        """Resolve a side-effecting (T1) tool against the run's effective autonomy (#218).

        ``act_auto`` ⇒ ALLOW (execute automatically); ``act_with_approval`` ⇒ APPROVE
        (route through the approval seam); ``suggest``/``draft`` ⇒ DENY (the assistant
        may only suggest/draft, not act). Only called for a T1 tool (the caller checks
        :func:`_requires_autonomy_approval`); ``risk_tier`` is threaded through for the
        approval request the caller builds.
        """
        if self._autonomy is AutonomyLevel.ACT_AUTO:
            return _AutonomyDecision.ALLOW
        if self._autonomy is AutonomyLevel.ACT_WITH_APPROVAL:
            return _AutonomyDecision.APPROVE
        # suggest / draft — below the act tiers: a side effect is not permitted.
        return _AutonomyDecision.DENY

    def _resolve(self, name: str) -> ToolDefinition | None:
        """Resolve ``name`` to a :class:`ToolDefinition`, or ``None`` (deny by default).

        A per-run dynamic MCP tool (``extra_tools``, ADR-0012 §6) takes precedence
        over the static ``impls/`` registry — the namespace ``mcp:*`` guarantees no
        collision with a native tool, so precedence only matters as a fast path. An
        unknown name (in neither) is ``None``, which the caller turns into a
        ``tool_not_found`` result.
        """
        dynamic = self._extra_tools.get(name)
        if dynamic is not None:
            return dynamic
        try:
            return get_tool(name)
        except UnknownToolError:
            return None

    async def _execute(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolContext,
        started: float,
    ) -> ToolResult:
        """Run the handler under its timeout, mapping any failure to an ok=False result."""
        try:
            body = await asyncio.wait_for(
                definition.handler(call.arguments, context),
                timeout=definition.timeout_seconds,
            )
        except TimeoutError:
            return ToolResult.failure(
                call_id=call.id,
                name=call.name,
                error=ERROR_TOOL_TIMEOUT,
                content="The tool timed out before returning a result.",
                summary="timed out",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001 — a throwing tool must not crash the run
            # Log the error *type* only (never the message — it may carry vendor
            # details); the model gets a safe, opaque tool reply (issue #207 §7).
            log.error("tool.failed", tool=call.name, error_type=type(exc).__name__)
            return ToolResult.failure(
                call_id=call.id,
                name=call.name,
                error=ERROR_TOOL_ERROR,
                content="The tool failed to complete. You may try a different approach.",
                summary="tool error",
                duration_ms=_elapsed_ms(started),
            )
        return _complete(call, body, _elapsed_ms(started))

    async def _finalise(
        self,
        *,
        call: ToolCall,
        args_hash: str,
        message_id: UUID | None,
        ordinal: int,
        result: ToolResult,
        outcome: AuditOutcome,
    ) -> ToolResult:
        """Audit (invoked + result) and record the ``tool_invocations`` row (AC-4).

        Runs for **every** outcome — success, denial, or failure — so INV-6 holds:
        an invocation with no emitted audit event or no trace row is impossible
        because this is the one exit through which every result returns.

        The ADR-0016 §5 **serialized persistence coordinator** (#412): the
        writes flush on the runner's one DB session (which admits no concurrent
        operations) AND must land in **dispatch order** — the append-only audit
        events carry no ordering column of their own, so their physical write
        sequence is the record. The condition gates each finalise until every
        lower ordinal has persisted; completions may land in any order, the
        writes never do. Both audit events also carry the ordinal in metadata,
        so dispatch order stays reconstructible even off-sequence readers.
        The ``finally`` advances the drain even when a write raises — the
        exception still propagates (the answer dies with a terminal), but a
        sibling finalise is never left waiting on a wedged ordinal.
        """
        metadata = {
            "tool": call.name,
            "call_id": call.id,
            "args_hash": args_hash,
            "ok": result.ok,
            "ordinal": ordinal,
            **({"error": result.error} if result.error else {}),
        }
        async with self._persist_gate:
            await self._persist_gate.wait_for(lambda: self._next_persist_ordinal == ordinal)
            try:
                await self._audit.emit(
                    action=AuditAction.TOOL_INVOKED,
                    actor=self._actor,
                    resource_type="tool",
                    resource_id=call.name,
                    outcome=outcome,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                    metadata=metadata,
                )
                await self._audit.emit(
                    action=AuditAction.TOOL_RESULT,
                    actor=self._actor,
                    resource_type="tool",
                    resource_id=call.name,
                    outcome=outcome,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                    metadata={**metadata, "duration_ms": result.duration_ms},
                )
                await self._invocations.record(
                    tool_name=call.name,
                    args_hash=args_hash,
                    ok=result.ok,
                    duration_ms=result.duration_ms,
                    session_id=self._session_id,
                    message_id=message_id,
                    error=result.error,
                    # The handler's user-safe result line ("3 passages", "13
                    # documents") persists so the thread trace can say what the
                    # tool returned (#377). Bounded by the repository; never
                    # raw args/payloads.
                    result_summary=result.summary,
                    ordinal=ordinal,
                )
            finally:
                self._next_persist_ordinal += 1
                self._persist_gate.notify_all()
        return result


def _complete(call: ToolCall, body: ToolHandlerResult, duration_ms: int) -> ToolResult:
    """Fold a handler body + timing into the uniform :class:`ToolResult`.

    A handler may itself return ``ok=False`` (a tool-specific rejection, e.g. a
    malformed id) with an ``error`` code; that is passed through. Otherwise the
    body is a success. Either way the runner owns ``call_id``/``duration_ms``.
    """
    if not body.ok:
        return ToolResult.failure(
            call_id=call.id,
            name=call.name,
            error=body.error or ERROR_TOOL_ERROR,
            content=body.content,
            summary=body.summary,
            duration_ms=duration_ms,
        )
    return ToolResult(
        call_id=call.id,
        name=call.name,
        ok=True,
        content=body.content,
        summary=body.summary,
        payload=body.payload,
        duration_ms=duration_ms,
        hit_count=body.hit_count,
        passages=body.passages,
        document_ids=body.document_ids,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["CallScopeFactory", "ToolRunner", "hash_args"]
