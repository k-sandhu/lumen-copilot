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
#: The tool handler raised — a safe, opaque message is surfaced (issue #207 §7).
ERROR_TOOL_ERROR = "tool_error"
#: The tool exceeded its per-call timeout (issue #207 §7).
ERROR_TOOL_TIMEOUT = "tool_timeout"
#: The tool arguments were malformed / rejected by the handler.
ERROR_BAD_ARGS = "tool_bad_args"


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
    """

    content: str
    ok: bool = True
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hit_count: int = 0
    passages: tuple[RetrievedPassage, ...] = ()
    document_ids: tuple[UUID, ...] = ()


__all__ = [
    "ERROR_APPROVAL_DENIED",
    "ERROR_BAD_ARGS",
    "ERROR_NOT_FOUND",
    "ERROR_NOT_PERMITTED",
    "ERROR_TOOL_ERROR",
    "ERROR_TOOL_TIMEOUT",
    "RiskTier",
    "ToolHandlerResult",
    "ToolResult",
]
