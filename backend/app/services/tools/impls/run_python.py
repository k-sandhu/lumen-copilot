"""The ``run_python`` tool — the agent's governed code-execution capability (issue #231).

Makes the merged sandbox engine (#230) **user-facing** as a governed agent tool: it
lets the assistant **write and run Python** to compute results and produce files
(stories E15-7 / E3-7 — analyse a dataset reproducibly, build a deliverable, not just
describe it). Given ``code`` (and an optional ``timeout_s`` wall-clock hint), the
handler submits the code to the sandbox **service/Celery task** through the narrow
:class:`~app.services.tools.types.SandboxToolRunner` seam (issue #231), awaits the
terminal ``code_run``, and returns a :class:`ToolResult` summarising the exit status,
the stdout/stderr tails, the produced artifact ids, and the ``code_run`` id (for
``GET /code-runs/{id}`` inspection). The seam streams ``code_output`` chunks and a
terminal ``code_result`` over the same chat stream (the frozen ADR-0013 contract).

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
the seam is not wired for this session (``ctx.sandbox is None``), the tenant disabled
code execution / is over quota (a ``denied`` run), a wall-clock ``timeout``, an
OOM/pids ``killed``, a non-zero-exit ``failed``, or an unexpected error inside the
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
#: Code execution is not enabled for this tenant/deploy, or the run was refused
#: before execution (over quota) — the sandbox never launched (ADR-0013 §6).
ERROR_CODE_EXECUTION_DENIED = "code_execution_denied"
#: The run exceeded its wall-clock budget and was killed (G6).
ERROR_CODE_EXECUTION_TIMEOUT = "code_execution_timeout"
#: The run was OOM-/pids-killed, or exited non-zero (a run-level failure).
ERROR_CODE_EXECUTION_FAILED = "code_execution_failed"

# How much of each captured stream to surface back to the model. The full,
# output-size-capped record is inspectable at GET /code-runs/{runId}; the tool reply
# stays compact so the context budget is not blown by a chatty run.
_TAIL_BUDGET = 2000


def _tail(text: str) -> str:
    """The trailing ``_TAIL_BUDGET`` chars of a captured stream (most-recent output)."""
    if len(text) <= _TAIL_BUDGET:
        return text
    return "…" + text[-_TAIL_BUDGET:]


def _clamp_timeout(value: object) -> int | None:
    """Coerce the model-supplied ``timeout_s`` to a positive int, or ``None`` (default).

    A missing/garbage value becomes ``None`` so the seam applies the configured
    wall-clock cap; the seam also clamps a supplied value to that ceiling, so a
    hostile large value can never widen the cap (G6).
    """
    if not isinstance(value, int | float | str) or isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


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

    timeout_s = _clamp_timeout(args.get("timeout_s"))

    # Submit through the narrow seam. The seam owns the whole crash-safe, audited run
    # lifecycle (create the queued code_run linked to the session/message, submit to
    # the merged sandbox service/task, stream code_output/code_result, await the
    # terminal), so tenant isolation (INV-1/INV-2) and the code_run.* audit (INV-6)
    # hold by delegation. A disabled/over-quota tenant comes back as a ``denied`` run,
    # NOT a raised exception — but a genuinely unexpected seam error must still be a
    # result, so the runner's bounded-execute wrapper (#207 §7) is the backstop.
    run = await ctx.sandbox.submit(code=code, timeout_s=timeout_s)

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

    # Every non-success terminal (denied / timeout / killed / failed) is an ok=False
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
            "report). The code runs with no network access and a wall-clock/memory "
            "budget; any files it writes to the output directory become downloadable "
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
                "timeout_s": {
                    "type": "integer",
                    "description": (
                        "Optional wall-clock budget in seconds for this run "
                        "(clamped to the configured ceiling)."
                    ),
                    "minimum": 1,
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
    ),
)


__all__ = [
    "ERROR_CODE_EXECUTION_DENIED",
    "ERROR_CODE_EXECUTION_FAILED",
    "ERROR_CODE_EXECUTION_TIMEOUT",
    "RUN_PYTHON_TOOL_NAME",
    "TOOLS",
]
