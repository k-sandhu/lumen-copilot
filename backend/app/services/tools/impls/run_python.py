"""The ``run_python`` tool — the agent's governed code-execution capability (issue #231).

Makes the merged sandbox engine (#230) **user-facing** as a governed agent tool: it
lets the assistant **write and run Python** to compute results and produce files
(stories E15-7 / E3-7 — analyse a dataset reproducibly, build a deliverable, not just
describe it). Given ``code`` and optional governed package requirements, the
handler submits the code to the sandbox **service/Celery task** through the narrow
:class:`~app.services.tools.types.SandboxToolRunner` seam (issue #231), awaits the
terminal ``code_run``, and returns a :class:`ToolResult` summarising the exit status,
the stdout/stderr tails, the produced artifact ids, and the ``code_run`` id (for
``GET /code-runs/{id}`` inspection). The seam streams ``code_output`` chunks and a
terminal ``code_result`` over the same chat stream (the additive ADR-0020 contract).

Governance — this is the most consequential tool the platform ships, so it is the
**highest tier** the platform uses, ``read_only=False``, and **admin-gated**:

* ``risk_tier=T2`` (spec 0004 §2.5 — consequential; executing arbitrary code is a
  write-tier action). ``T2`` ⇒ ``requires_approval=True`` is a build-time guarantee
  (``ToolDefinition.__post_init__``), so the governed runner routes every invocation
  through the ``ApprovalGate`` **before** the handler runs (INV-7). The default
  ``DenyAllApprovalGate`` denies fail-closed, so an unapproved run is never executed.
* ``default_offered=False`` keeps it out of the ad-hoc chat default allow-list
  (``registry.default_allowlist``): it is offered to the model only when an
  assistant's allow-list includes it, and even then only when the tenant/deploy has
  enabled code execution (``SANDBOX_ENABLED`` / the per-tenant enable #233). Enabling
  it is a deliberate allow-list + admin decision (deny-by-default preserved).

**Failure is a result, never a crash** (issue #207 §7). Every non-happy outcome —
the seam is not wired for this session (``ctx.sandbox is None``), code execution is
disabled or a package is denied (a ``denied`` run), an explicitly cancelled
``killed`` run, a non-zero-exit ``failed`` run, or an unexpected error inside the
seam — is folded into an ``ok=False`` :class:`ToolHandlerResult` the model reads and
can recover from (fix the code, re-run within the tool-turn budget). The real sandbox
isolation is #230's live-gated coverage; this handler only submits and renders.
"""

from __future__ import annotations

from typing import Any

from app.domain.entities import CodeRunStatus
from app.domain.tools import ERROR_BAD_ARGS, RiskTier, ToolHandlerResult
from app.services.tools.types import SandboxRun, ToolContext, ToolDefinition

#: The registered name of the code-execution tool — the single source of truth the
#: chat runtime keys off when deciding whether to wire the sandbox seam (a session
#: that does not offer ``run_python`` gets no execution plumbing).
RUN_PYTHON_TOOL_NAME = "run_python"

# Stable ``ok=False`` codes distinct from the generic runner codes, so the model (and
# the trace) can tell *why* a run did not succeed — mirroring ``web_search``'s codes.
#: Code execution is not enabled for this tenant/deploy, or package policy refused
#: the request before tenant code launched (ADR-0020 §3).
ERROR_CODE_EXECUTION_DENIED = "code_execution_denied"
#: Historical compatibility code for a timeout row (ADR-0020 adds no new timeouts).
ERROR_CODE_EXECUTION_TIMEOUT = "code_execution_timeout"
#: The run was explicitly killed or exited non-zero (a run-level failure).
ERROR_CODE_EXECUTION_FAILED = "code_execution_failed"

# How much of each captured stream to surface back to the model. The full,
# captured record is inspectable at GET /code-runs/{runId}; only the model-facing
# reply is tailed so a chatty run does not consume the conversation context.
_TAIL_BUDGET = 2000


def _tail(text: str) -> str:
    """The trailing ``_TAIL_BUDGET`` chars of a captured stream (most-recent output)."""
    if len(text) <= _TAIL_BUDGET:
        return text
    return "…" + text[-_TAIL_BUDGET:]


def _packages(value: object) -> tuple[str, ...] | None:
    """Return a bounded list of non-empty package requirement strings, or invalid."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 50:
        return None
    if any(not isinstance(item, str) or not item.strip() or len(item) > 300 for item in value):
        return None
    return tuple(item.strip() for item in value)


def _rendered(run: SandboxRun) -> str:
    """Render a finished run as the tool reply the model reads.

    Carries the exit status, the stdout/stderr tails, the produced artifact ids, and
    the ``code_run`` id so the model can reference the inspectable record. Kept within
    the tail budget; the full record lives at ``GET /code-runs/{runId}``.
    """
    lines = [
        f"Code run {run.code_run_id} finished with status {run.status.value}"
        f" (exit code {run.exit_code}).",
    ]
    if run.stdout:
        lines.append(f"stdout:\n{_tail(run.stdout)}")
    if run.stderr:
        lines.append(f"stderr:\n{_tail(run.stderr)}")
    if run.artifact_ids:
        ids = ", ".join(str(a) for a in run.artifact_ids)
        lines.append(f"Produced {len(run.artifact_ids)} artifact(s): {ids}")
    return "\n\n".join(lines)


def _payload(run: SandboxRun) -> dict[str, Any]:
    """The structured result body persisted as jsonb + read by the runtime/UI."""
    return {
        "runId": str(run.code_run_id),
        "status": run.status.value,
        "exitCode": run.exit_code,
        "durationMs": run.duration_ms,
        "artifactIds": [str(a) for a in run.artifact_ids],
        "sandboxSessionId": (
            str(run.sandbox_session_id) if run.sandbox_session_id is not None else None
        ),
        "sandboxGeneration": run.sandbox_generation,
    }


def _error_for(status: CodeRunStatus) -> str:
    """Map a non-success terminal status to the stable ``ok=False`` code (fail-closed)."""
    if status is CodeRunStatus.DENIED:
        return ERROR_CODE_EXECUTION_DENIED
    if status is CodeRunStatus.TIMEOUT:
        return ERROR_CODE_EXECUTION_TIMEOUT
    # failed / killed (and any unexpected non-terminal-success) → a run-level failure.
    return ERROR_CODE_EXECUTION_FAILED


async def _run_python(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    """Submit ``code`` to the sandbox and summarise the terminal run (issue #231)."""
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return ToolHandlerResult(
            content="The 'code' argument must be a non-empty Python source string.",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="no code",
        )

    # A session that does not offer code execution is never wired with the seam
    # (deny-by-default: run_python is off the ad-hoc allow-list and admin-gated). If
    # it is somehow invoked without one, report a typed result — never crash.
    if ctx.sandbox is None:
        return ToolHandlerResult(
            content=(
                "Code execution is not available in this session. "
                "Answer from the connected documents and tools instead."
            ),
            ok=False,
            error=ERROR_CODE_EXECUTION_DENIED,
            summary="code execution unavailable",
        )

    packages = _packages(args.get("packages"))
    if packages is None:
        return ToolHandlerResult(
            content=(
                "The 'packages' argument must be an array of at most 50 non-empty "
                "PEP-508 requirement strings."
            ),
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="invalid packages",
        )

    # Submit through the narrow seam. The seam owns the whole crash-safe, audited run
    # lifecycle (create the queued code_run linked to the session/message, submit to
    # the merged sandbox service/task, stream code_output/code_result, await the
    # terminal), so tenant isolation (INV-1/INV-2) and the code_run.* audit (INV-6)
    # hold by delegation. A disabled/package-denied request comes back as a ``denied`` run,
    # NOT a raised exception — but a genuinely unexpected seam error must still be a
    # result, so the runner's bounded-execute wrapper (#207 §7) is the backstop.
    run = await ctx.sandbox.submit(code=code, packages=packages)

    if run.status is CodeRunStatus.SUCCEEDED:
        return ToolHandlerResult(
            content=_rendered(run),
            summary=(
                f"code run succeeded ({len(run.artifact_ids)} artifact(s))"
                if run.artifact_ids
                else "code run succeeded"
            ),
            payload=_payload(run),
        )

    # Every non-success terminal (denied / historical timeout / killed / failed) is an ok=False
    # result the model can recover from — fix the code and re-run within the budget.
    return ToolHandlerResult(
        content=_rendered(run),
        ok=False,
        error=_error_for(run.status),
        summary=f"code run {run.status.value}",
        payload=_payload(run),
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name=RUN_PYTHON_TOOL_NAME,
        description=(
            "Write and run Python in an isolated sandbox to compute a result or "
            "produce a file (e.g. analyse an uploaded dataset, build a chart or a "
            "report). The chat reuses one isolated, writable environment across turns. "
            "Code runs as root inside it, with no model-code network route, host mounts, "
            "engine socket, or application secrets. Policy-admitted packages persist; "
            "any files it writes to the output directory become downloadable "
            "artifacts. Returns the exit status, the stdout/stderr tails, and the ids "
            "of any artifacts produced. Use this to actually run analysis, not just "
            "describe it."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source to execute in the sandbox.",
                },
                "packages": {
                    "type": "array",
                    "description": (
                        "Optional PEP-508 package requirements to install as root in "
                        "the reusable session, subject to the tenant package policy."
                    ),
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                },
            },
            "required": ["code"],
        },
        handler=_run_python,
        # The HIGHEST tier the tool platform uses (spec 0004 §2.5): executing
        # arbitrary code is consequential (T2). T2 ⇒ requires_approval is enforced at
        # construction (INV-7 structural) — the approval gate runs before the handler.
        risk_tier=RiskTier.T2,
        requires_approval=True,
        read_only=False,
        # Off by default: admin/assistant-gated, NOT part of the ad-hoc default
        # allow-list. Offered only via an assistant allow-list, and only when the
        # tenant/deploy enabled code execution (SANDBOX_ENABLED / the #233 enable).
        default_offered=False,
        # ADR-0020 deliberately has no automatic execution timeout. Explicit
        # cancellation/reset is the recovery path for an unbounded run.
        timeout_seconds=None,
    ),
)


__all__ = [
    "ERROR_CODE_EXECUTION_DENIED",
    "ERROR_CODE_EXECUTION_FAILED",
    "ERROR_CODE_EXECUTION_TIMEOUT",
    "RUN_PYTHON_TOOL_NAME",
    "TOOLS",
]
