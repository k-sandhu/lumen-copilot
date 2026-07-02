"""The governed tool runner (CC-7 #207) — the single tool-invocation chokepoint.

Every tool call in the system flows through :meth:`ToolRunner.run`, and there is
**no other path** to invoke a tool (AC-N). In one place it enforces the whole
governance contract, in this fixed order:

1. **Resolve** the tool in the registry — an unknown name → ``tool_not_found``.
2. **Allow-list** (issue #207 §2 / AC-2): a tool not in the per-run allowed set →
   ``tool_not_permitted``. Enforced here, once — no bypass.
3. **Approval seam** (issue #207 §3 / AC-3 / INV-7): a ``requires_approval`` tool
   is routed to the :class:`~app.services.tools.types.ApprovalGate` and blocks
   until approved; a denial → ``approval_denied`` and the handler never runs.
   T0/T1 tools skip the gate.
4. **Bounded execute** (issue #207 §7 / AC-5): the handler runs under a per-tool
   timeout; a raised or timed-out handler → an ``ok=False`` result with a safe
   message — never a crashed stream.
5. **Uniform result** (issue #207 §4): whatever happened, produce one
   :class:`~app.domain.tools.ToolResult`.
6. **Audit + trace** (issue #207 §4/§5 / AC-4 / INV-6): emit ``tool.invoked``
   (intent) and ``tool.result`` (outcome) through the one audit sink, and write a
   ``tool_invocations`` row — for **every** invocation, including a governance
   denial or a failure, so the trace has no silent gap.

Layering (ADR-0004): ``services/`` orchestration. It composes the registry, the
approval gate, the ``db/`` ``ToolInvocationRepository``, and the audit sink; it
returns a domain :class:`ToolResult` and leaks no adapter/vendor type upward.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from uuid import UUID

from app.core.logging import get_logger
from app.db.repositories import ToolInvocationRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.llm import ToolCall
from app.domain.tools import (
    ERROR_APPROVAL_DENIED,
    ERROR_NOT_FOUND,
    ERROR_NOT_PERMITTED,
    ERROR_TOOL_ERROR,
    ERROR_TOOL_TIMEOUT,
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
    ) -> None:
        self._allowed = allowed
        self._invocations = invocations
        self._audit = audit
        self._actor = actor
        self._request_id = request_id
        self._source_ip = source_ip
        self._session_id = session_id
        self._gate: ApprovalGate = gate or DenyAllApprovalGate()

    async def run(
        self, *, call: ToolCall, context: ToolContext, message_id: UUID | None = None
    ) -> ToolResult:
        """Invoke ``call`` through the governed path; always return a :class:`ToolResult`.

        Never raises for a tool concern (an unknown/off-list/unapproved/failing
        tool is an ``ok=False`` result the model reads and the run recovers from).
        Emits ``tool.invoked`` before and ``tool.result`` after, and writes one
        ``tool_invocations`` row — for every outcome (AC-4/INV-6).
        """
        args_hash = hash_args(call.arguments)
        started = time.monotonic()

        # (1) Resolve. An unknown tool is denied by default (deny-by-default), and
        # never even reaches the allow-list/handler.
        try:
            definition: ToolDefinition | None = get_tool(call.name)
        except UnknownToolError:
            definition = None

        # (2) Allow-list — the one enforcement point (AC-2 / AC-N). A tool absent
        # from the registry OR absent from this run's allowed set is refused with a
        # typed result; the handler is not invoked.
        if definition is None:
            return await self._finalise(
                call=call,
                args_hash=args_hash,
                message_id=message_id,
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

        # (3) Approval seam (AC-3 / INV-7). Only ``requires_approval`` (⇒ T2+) tools
        # are gated; T0/T1 bypass it entirely. A denial refuses the call BEFORE the
        # handler runs — no consequential action executes without approval.
        if definition.requires_approval:
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

        # (4) Bounded execute (AC-5). A raised/timed-out handler becomes an
        # ok=False result — the stream never crashes.
        result = await self._execute(definition, call, context, started)
        outcome = AuditOutcome.ALLOWED if result.ok else AuditOutcome.ERROR
        return await self._finalise(
            call=call,
            args_hash=args_hash,
            message_id=message_id,
            result=result,
            outcome=outcome,
        )

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
        result: ToolResult,
        outcome: AuditOutcome,
    ) -> ToolResult:
        """Audit (invoked + result) and record the ``tool_invocations`` row (AC-4).

        Runs for **every** outcome — success, denial, or failure — so INV-6 holds:
        an invocation with no emitted audit event or no trace row is impossible
        because this is the one exit through which every result returns.
        """
        metadata = {
            "tool": call.name,
            "call_id": call.id,
            "args_hash": args_hash,
            "ok": result.ok,
            **({"error": result.error} if result.error else {}),
        }
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
        )
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


__all__ = ["ToolRunner", "hash_args"]
