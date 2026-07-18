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
from app.domain.entities import Artifact, ArtifactProducedBy, CodeRunStatus
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
class SandboxRun:
    """The terminal outcome of a governed ``run_python`` submission (issue #231).

    The narrow domain shape the sandbox seam hands back to the ``run_python`` tool
    once a code run reaches a terminal :class:`~app.domain.entities.CodeRunStatus`:
    the ``code_run_id`` (for ``GET /code-runs/{id}`` inspection and the WS
    ``runId`` correlation), the terminal ``status``/``exit_code``/``duration_ms``,
    the captured ``stdout``/``stderr`` tails the tool summarises
    back to the model, and the ids of any artifacts the run produced (collected via
    CC-B #208). It carries **no** runner/Docker wire object — the sandbox boundary
    exposes domain types only (ADR-0004).
    """

    code_run_id: UUID
    status: CodeRunStatus
    exit_code: int | None
    duration_ms: int | None
    stdout: str
    stderr: str
    artifact_ids: tuple[UUID, ...] = ()
    sandbox_session_id: UUID | None = None
    sandbox_generation: int | None = None


@runtime_checkable
class SandboxToolRunner(Protocol):
    """The narrow code-execution seam the ``run_python`` tool submits through (#231).

    A tool never orchestrates a container, never talks to the ``sandbox-runner``,
    and never enqueues a Celery task itself (ADR-0004: the ``sandbox/`` module owns
    code execution, ``tasks/`` owns enqueue). This Protocol exposes one method:
    submit the model-authored ``code`` to the merged sandbox engine (#230), await
    the terminal :class:`~app.domain.entities.CodeRun`, and return a
    :class:`SandboxRun` summary — streaming ``code_output`` chunks and a terminal
    ``code_result`` over the chat stream as the run progresses (the ADR-0020
    contract). The real implementation is already scoped to the caller's tenant +
    owner (from the principal, never tool args), so tenant isolation (INV-1/INV-2)
    and the ``code_run.*`` audit (INV-6) hold by delegation; a test fake implements
    only this one method.

    The seam is **admin-gated at the deploy/tenant layer** (``SANDBOX_ENABLED`` /
    the per-tenant enable #233): a disabled tenant's submission comes back as a
    ``denied`` :class:`SandboxRun` (never a raised exception), which the tool folds
    into an ``ok=False`` result. A submission that never even reaches a run (the
    seam is not wired for this session) is the tool's ``sandbox is None`` guard.
    """

    async def submit(self, *, code: str, packages: tuple[str, ...] = ()) -> SandboxRun:
        """Run ``code`` in the sandbox to a terminal status; return its outcome.

        Streams ``code_output`` chunks and a terminal ``code_result`` over the chat
        stream, correlated by the ``runId`` (the ``code_run`` id). Approved package
        requirements are installed as root and persist in the chat's generation.
        """
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

    ``sandbox`` is the optional #231 code-execution seam the ``run_python`` tool
    submits through. It defaults ``None`` — a session that does not offer
    ``run_python`` (the deny-by-default case; ``run_python`` is off the ad-hoc
    allow-list and admin-gated) is never wired with a runner, so no untested
    execution plumbing hangs off a path that cannot use it, and the tool reports a
    typed ``ok=False`` if ever invoked without the seam rather than crashing.
    """

    principal: Principal
    retrieval: RetrievalService
    collection_ids: list[UUID] | None = None
    #: Pinned documents (spec 0007 #429): when set, passage search narrows to
    #: these ids — an ADDITIONAL filter over the caller's allow-set, applied
    #: inside ``retrieval/`` (INV-2 unchanged; an inaccessible id contributes
    #: nothing and discloses nothing). ``None`` ⇒ unchanged behavior.
    document_ids: list[UUID] | None = None
    default_k: int = 6
    #: The enforceable CEILING on a single search's ``k`` (ADR-0016 §1 / #410):
    #: the context assembler derives it from the input budget and a retrieval tool
    #: clamps even an explicit, model-supplied ``k`` to it, so a search issued
    #: after assembly cannot overflow the window it was budgeted against. Defaults
    #: to the widest retrieval ceiling (50, ``list_documents``'s cap) — each tool
    #: still clamps to its OWN max, so this is inert on a roomy run and only bites
    #: when a tight budget lowers it.
    max_k: int = 50
    #: The per-passage snippet char budget a search renders into the transcript
    #: (ADR-0016 §1 / #410): the assembler lowers it under a tight window so each
    #: returned passage adds less to the context. Defaults to the retrieval tool's
    #: own snippet cap (600) — inert unless a tighter budget lowers it.
    snippet_budget: int = 600
    artifacts: ArtifactWriter | None = None
    session_id: UUID | None = None
    simulate_writes: bool = False
    sandbox: SandboxToolRunner | None = None


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

    ``default_offered`` governs whether the tool is part of the **ad-hoc chat
    default allow-list** (``registry.default_allowlist``). It defaults ``True`` so
    the read-only retrieval tools stay the ad-hoc default; a tool that is
    **off-by-default and admin/assistant-gated** (e.g. ``web_search`` — reaches
    outside the tenant's corpus and needs an explicit web-mode enable per ADR-0014
    §5) sets it ``False``, so a newly added T0 tool is NOT silently granted to
    every ad-hoc session — enabling it is a deliberate allow-list change
    (deny-by-default; issue #219). The runner's allow-list is still the hard
    chokepoint; this only decides what the *default* set offers.
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    handler: ToolHandler
    risk_tier: RiskTier = RiskTier.T0
    requires_approval: bool = False
    read_only: bool = True
    timeout_seconds: float | None = 15.0
    default_offered: bool = True

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
    "SandboxRun",
    "SandboxToolRunner",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
]
