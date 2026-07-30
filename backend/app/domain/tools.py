"""Domain types for the governed tool platform (CC-7 #207, spec 0004 §2.5).

The **pure** vocabulary of the agent tool platform — the read-side gateway every
tool (retrieval today; web-search / file-write / ``run_python`` / MCP later) plugs
into. This module holds only the parts with no adapter/``auth`` dependency: the
**risk tier** (spec 0004 §2.5), the uniform :class:`ToolResult` / handler-body
:class:`ToolHandlerResult`, and the typed governance **error codes**. The
adapter-bound pieces (``ToolContext`` carrying the ``retrieval/`` service,
``ToolDefinition`` + its handler, and the ``ApprovalGate`` seam that references a
``Principal``) live one layer out in ``services/tools/`` so ``domain/`` stays free
of framework/adapter imports (backend/AGENTS.md: ``domain/`` is pure).

Risk tiers are encoded here so the read-before-write invariant (mission filter #3,
INV-7) is structural, not prose: a ``requires_approval`` tool must clear the
approval gate (in ``services/tools/``) before it can act; the whole MVP is T0, so
the gate is wired but inert for the tools that ship. The negative invariant holds
by construction — the default gate denies every request, so no T2+ tool can
execute without a recorded approval.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.domain.retrieval import RetrievedPassage


class RiskTier(str, enum.Enum):
    """The read-before-write risk tier of a tool (spec 0004 §2.5).

    * ``T0`` — read-only (retrieve / answer / summarize / draft-in-chat). The
      entire MVP is T0.
    * ``T1`` — reversible internal write (create collection, upload/delete *own*
      document): authorized owner, audited, no extra approval.
    * ``T2`` — consequential / external write (write-back, send, external share):
      **explicit human approval** in-session + stated risk tier — out of MVP.
    * ``T3`` — destructive / irreversible external (bulk delete, change source
      permissions): approval **+** confirmation — out of MVP.

    Ordered so ``is_write_tier`` expresses "T2 and above needs the approval gate"
    without a lookup table.
    """

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"

    @property
    def level(self) -> int:
        """The numeric tier (0–3) — ``T2``/``T3`` are the write tiers (≥ 2)."""
        return int(self.value[1:])

    @property
    def is_write_tier(self) -> bool:
        """True for T2/T3 — the consequential tiers that require approval (§2.5)."""
        return self.level >= RiskTier.T2.level


# --- Error codes -----------------------------------------------------------
# The stable, typed reasons a tool call resolves to an ``ok=False`` result. The
# model sees these strings in the tool reply and the run continues — a governance
# denial or a tool failure is a *result*, never an exception that crashes the
# stream (issue #207 §2/§7). Persisted in ``tool_invocations.error`` for the trace.

#: The tool is not in the session/assistant allow-list (issue #207 §2 / AC-2).
ERROR_NOT_PERMITTED = "tool_not_permitted"
#: No tool with that name is registered (deny by default — an unknown tool).
ERROR_NOT_FOUND = "tool_not_found"
#: A ``requires_approval`` tool whose approval was denied / not granted (INV-7).
ERROR_APPROVAL_DENIED = "approval_denied"
#: A side-effecting (T1) tool the assistant's EFFECTIVE autonomy does not permit —
#: a ``suggest``/``draft`` assistant cannot execute a write without stepping up to
#: ``act_with_approval`` / ``act_auto`` (issue #218, ADR-0011 §3). Distinct from an
#: approval denial: the *level* forbids the action, not a missing approval.
ERROR_AUTONOMY_DENIED = "autonomy_denied"
#: The tool handler raised — a safe, opaque message is surfaced (issue #207 §7).
ERROR_TOOL_ERROR = "tool_error"
#: The tool exceeded its per-call timeout (issue #207 §7).
ERROR_TOOL_TIMEOUT = "tool_timeout"
#: The tool arguments were malformed / rejected by the handler.
ERROR_BAD_ARGS = "tool_bad_args"


# --- Approval refusal reasons (issue #502; the #500 honesty fix) ------------
# ``ERROR_APPROVAL_DENIED`` is the STABLE code every approval refusal keeps —
# these are the ADDITIVE sub-reasons that say WHICH switch refused, so an
# operator reading a blocked run can tell "nobody enabled this tool" from "an
# admin ticked requires-approval, which no surface can satisfy". They ride on
# the ``ApprovalDecision`` the gate returns and land in the model-facing tool
# reply, the ``tool_invocations`` row, and the ``tool.invoked``/``tool.result``
# audit metadata (``denied_reason``).

#: No admin tool-policy row exists for the tool (deny-by-default, issue #223).
APPROVAL_REASON_POLICY_ABSENT = "tool_policy_absent"
#: An admin row exists and says ``enabled=false`` — the tenant turned it off.
APPROVAL_REASON_POLICY_DISABLED = "tool_policy_disabled"
#: The tool is ENABLED but still flagged ``requires_approval`` — and no surface
#: can grant that approval (issue #500). Mechanically a permanent deny, so it
#: must never read as "disabled": the fix is to clear the flag, not to enable.
#: The interactive approval flow itself is #501 (spec first) and does not exist.
APPROVAL_REASON_APPROVAL_UNAVAILABLE = "approval_required_unavailable"
#: The tool policy could not be read; the call was refused fail-closed (INV-7).
APPROVAL_REASON_POLICY_UNREADABLE = "tool_policy_unreadable"
#: No real approval gate is wired in this deployment (the inert deny-all default).
APPROVAL_REASON_GATE_INERT = "approval_gate_inert"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The uniform result of one tool invocation (issue #207 §4).

    Every tool — success, governance denial, or failure — yields exactly this
    shape, so the runtime, the trace, the ``tool_invocations`` row, and (later)
    AgentOps all read one contract. ``ok`` is the headline; ``summary`` is the
    short human/trace line; ``payload`` is the structured result body persisted as
    jsonb; ``content`` is the text fed back to the model as the tool reply.
    ``error`` is set (to one of the ``ERROR_*`` codes) **iff** ``ok`` is False.

    ``passages`` / ``document_ids`` / ``hit_count`` carry the retrieval-specific
    provenance the chat runtime turns into citations (INV-3) and audit metadata;
    they are empty for non-retrieval tools. ``duration_ms`` is the wall-clock the
    runner measured (bounded by the per-tool timeout).

    ``denied_reason`` (issue #502) is the typed sub-reason behind a governance
    refusal, when the refusing layer knows one — the specific switch that said no,
    not just that something did. The runner carries it into the audit metadata
    beside the ``error`` code. The approval gate supplies it from
    :class:`~app.services.tools.types.ApprovalDecision`; a handler that is itself
    reporting a governed refusal (``run_python`` folding a ``denied`` code run)
    sets it on its :class:`ToolHandlerResult`.

    Invariant: a well-formed result has ``ok`` XOR ``error`` — ``ok=True`` ⇒
    ``error is None``; ``ok=False`` ⇒ ``error`` is a non-empty code.
    """

    call_id: str
    name: str
    ok: bool
    content: str
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    hit_count: int = 0
    passages: tuple[RetrievedPassage, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    denied_reason: str | None = None

    def __post_init__(self) -> None:
        # Structural guard for the ok XOR error invariant (issue #207 §4): a
        # success must not carry an error code, and a failure must name one — so a
        # persisted row can never be ambiguous about the outcome.
        if self.ok and self.error is not None:
            raise ValueError("a successful ToolResult must not carry an error code")
        if not self.ok and not self.error:
            raise ValueError("a failed ToolResult must carry a non-empty error code")

    @classmethod
    def failure(
        cls,
        *,
        call_id: str,
        name: str,
        error: str,
        content: str,
        summary: str | None = None,
        duration_ms: int = 0,
        denied_reason: str | None = None,
    ) -> ToolResult:
        """Build an ``ok=False`` result carrying ``error`` (a governance denial or failure)."""
        return cls(
            call_id=call_id,
            name=name,
            ok=False,
            content=content,
            summary=summary if summary is not None else error,
            error=error,
            duration_ms=duration_ms,
            denied_reason=denied_reason,
        )


@dataclass(frozen=True, slots=True)
class ToolHandlerResult:
    """The body a tool handler returns; the runner completes it into a :class:`ToolResult`.

    A handler concerns itself only with *what it did* — the ``content`` the model
    reads, a ``summary``, structured ``payload``, and (for retrieval tools) the
    permitted ``passages``/``document_ids``. It never sets ``call_id`` /
    ``duration_ms`` / governance ``error`` — the runner owns those. ``ok`` defaults
    True; a handler sets it False (with an ``error`` code) only for a
    tool-specific rejection (e.g. malformed args), which the runner passes through.

    ``denied_reason`` (issue #502) is the one governance field a handler *may* set:
    when the refusal happened *below* the handler and it is only relaying one (the
    ``run_python`` tool folding a ``denied`` code run), it passes the typed reason up
    so the durable ``tool_invocations`` row and the audit metadata record which
    switch refused, not merely that one did.
    """

    content: str
    ok: bool = True
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hit_count: int = 0
    passages: tuple[RetrievedPassage, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    denied_reason: str | None = None


__all__ = [
    "APPROVAL_REASON_APPROVAL_UNAVAILABLE",
    "APPROVAL_REASON_GATE_INERT",
    "APPROVAL_REASON_POLICY_ABSENT",
    "APPROVAL_REASON_POLICY_DISABLED",
    "APPROVAL_REASON_POLICY_UNREADABLE",
    "ERROR_APPROVAL_DENIED",
    "ERROR_AUTONOMY_DENIED",
    "ERROR_BAD_ARGS",
    "ERROR_NOT_FOUND",
    "ERROR_NOT_PERMITTED",
    "ERROR_TOOL_ERROR",
    "ERROR_TOOL_TIMEOUT",
    "RiskTier",
    "ToolHandlerResult",
    "ToolResult",
]
