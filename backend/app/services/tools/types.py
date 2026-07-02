"""Adapter-bound tool-platform types (CC-7 #207) — the ``services/`` half.

The pieces of the tool vocabulary that reference an adapter or the ``auth``
principal, and so cannot live in the pure ``domain/tools`` module (backend/AGENTS.md:
``domain/`` is pure): the permission-scoped :class:`ToolContext` (which carries the
``retrieval/`` service), the :class:`ToolDefinition` + its handler signature, and
the :class:`ApprovalGate` seam (which references a :class:`~app.auth.principal.Principal`).

A tool is a :class:`ToolDefinition`: identity + JSON-Schema, governance metadata
(risk tier per spec 0004 §2.5, ``requires_approval``, ``read_only``), and a handler.
Governance consistency is checked at construction so a miscategorised tool is a
registration-time error, not a runtime surprise (INV-7 is structural).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.auth.principal import Principal
from app.domain.entities import Artifact, ArtifactProducedBy
from app.domain.tools import RiskTier, ToolHandlerResult
from app.retrieval import RetrievalService
from app.services.artifacts_service import ArtifactLinks


@runtime_checkable
class ArtifactWriter(Protocol):
    """The narrow artifact-write seam a T1 file-writing tool persists through (#220).

    A tool never constructs a storage client (issue #220) — it writes through the
    #208 ``ArtifactsService``, which is already scoped to the caller's tenant +
    owner at construction (``tenant_id``/``owner_id`` come from the principal,
    never request input). This Protocol exposes only ``create_artifact`` so a
    write tool cannot reach the read/list/delete surface; the real
    :class:`~app.services.artifacts_service.ArtifactsService` satisfies it
    structurally, and a test fake need only implement this one method.

    Validation (allowlist / size cap) and the ``artifact.created`` audit both live
    inside the service (CC-B), so tenant isolation and INV-6 are preserved by
    delegation — the tool only supplies the bytes + metadata.
    """

    async def create_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        produced_by: ArtifactProducedBy,
        links: ArtifactLinks | None = None,
    ) -> Artifact:
        """Validate, store, and register a produced artifact; return its row."""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The permission-scoped context a tool handler runs against (issue #207).

    Carries *who* is asking (``principal`` — the tenant + user the ``retrieval/``
    filter keys off, INV-2) and the collaborators a tool may use. It is the only
    handle a handler gets: a tool never reaches an unfiltered adapter, only the
    permission-filtered ``retrieval`` service and the request-scoped knobs
    (``collection_ids``/``default_k``). New tool families extend this additively
    (e.g. a future ``http`` client) without widening any existing tool's reach.

    ``artifacts`` is the optional #208 write seam a T1 file-writing tool persists
    through (``None`` for a read-only run that offers no write tool — the handler
    then reports a typed ``ok=False`` rather than crashing). ``simulate_writes``
    is the read-only test-mode flag (F-AB-5): when set, a write tool builds and
    validates the bytes but does **not** persist — the artifact-producing action is
    simulated, so the read-only harness never mutates state.
    """

    principal: Principal
    retrieval: RetrievalService
    collection_ids: list[UUID] | None = None
    default_k: int = 6
    artifacts: ArtifactWriter | None = None
    session_id: UUID | None = None
    simulate_writes: bool = False


# The signature every tool handler satisfies: given the model-supplied ``args`` and
# the permission-scoped :class:`ToolContext`, produce a partial result body
# (:class:`~app.domain.tools.ToolHandlerResult`). The handler returns a normal
# ``ok=True`` empty result for a "no results" case — it does not raise for that;
# the runner wraps its own ``call_id``/``duration_ms``/timeout/exception handling
# around it (issue #207 §4/§7), turning a raised handler into an ``ok=False`` result.
ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolHandlerResult]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A governed tool the model may call (issue #207 §1).

    Declares the tool's identity + schema (``name`` / ``description`` /
    ``json_schema`` — the JSON Schema for its arguments, advertised to the model),
    its governance metadata (``risk_tier`` per spec 0004 §2.5, ``requires_approval``,
    ``read_only``), and the ``handler`` that runs it. Frozen so a registered
    definition cannot be mutated after discovery.

    Governance consistency (checked at construction): a ``read_only`` tool is
    ``T0`` and never ``requires_approval``; a write-tier (T2/T3) tool
    ``requires_approval`` (INV-7 — no consequential action without approval). The
    ``timeout_seconds`` bounds a single invocation so a slow tool cannot stall the
    run (issue #207 §7).
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    handler: ToolHandler
    risk_tier: RiskTier = RiskTier.T0
    requires_approval: bool = False
    read_only: bool = True
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("a tool definition requires a non-empty name")
        # A read-only tool is by definition T0 and never gated — encode it so a
        # miscategorised tool is a registration-time error, not a runtime surprise.
        if self.read_only and self.risk_tier is not RiskTier.T0:
            raise ValueError(f"read-only tool {self.name!r} must be risk tier T0")
        if self.read_only and self.requires_approval:
            raise ValueError(f"read-only tool {self.name!r} cannot require approval")
        # INV-7 structural guard: a consequential (T2+) tool MUST require approval,
        # so there is no way to register a write-tier tool that bypasses the gate.
        if self.risk_tier.is_write_tier and not self.requires_approval:
            raise ValueError(
                f"write-tier tool {self.name!r} ({self.risk_tier.value}) must require approval"
            )


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A pending approval for a ``requires_approval`` tool call (the CC-7 seam).

    Emitted before a gated tool is invoked; the approval gate decides allow/deny.
    Carries the identity of the call so an out-of-band approver (the F-ADMIN-TOOLS
    UI, later) can correlate it. The MVP ships no such tool, so the default gate
    denies every request (the negative INV-7 assertion).
    """

    call_id: str
    tool_name: str
    risk_tier: RiskTier
    principal: Principal
    arguments: dict[str, Any]


@runtime_checkable
class ApprovalGate(Protocol):
    """The read-before-write chokepoint (CC-7, spec 0004 §2.5 / INV-7).

    A ``requires_approval`` tool call is routed here *before* it can execute; the
    gate returns True only if the action is approved. T0/T1 tools bypass the gate
    entirely (the runner never calls it for them). The seam is built now though no
    T2+ tool ships in v1 — the default implementation denies everything, so the
    invariant "no unapproved consequential action" holds by construction, and a
    future approval flow (F-ADMIN-TOOLS) is a swap of this one collaborator.
    """

    async def request(self, request: ApprovalRequest) -> bool:
        """Return True iff the gated action is approved; False denies it."""
        ...


class DenyAllApprovalGate:
    """The default approval gate: deny every gated request (fail-closed, INV-7).

    The MVP ships only T0 tools, so no call reaches the gate on the happy path;
    but if a ``requires_approval`` tool is ever invoked without a real approval
    flow wired, this denies it (the run continues with an ``approval_denied``
    result) rather than silently executing a consequential action. This is the
    inert-by-default gate the spec calls for — a real gate replaces it later.
    """

    async def request(self, request: ApprovalRequest) -> bool:
        return False


__all__ = [
    "ApprovalGate",
    "ApprovalRequest",
    "ArtifactWriter",
    "DenyAllApprovalGate",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
]
