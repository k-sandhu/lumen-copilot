"""The ``write_file`` tool — the agent's file-writing capability (issue #220).

A **T1** (reversible internal write, [spec 0004 §2.5]) tool that turns model
output into a durable, downloadable **artifact**: the deliverable itself is
produced, not just described (stories E5-5 / E9-4). Given a ``filename``, a
``content_type``, and the ``content`` (text, or base64 for a binary format), the
handler builds the bytes and persists them through the #208
:class:`~app.services.artifacts_service.ArtifactsService` (CC-B) — the tool never
constructs a storage client (issue #220): the service is already scoped to the
caller's tenant + owner, validates the allowlist/size cap, and audits
``artifact.created``. So tenant isolation (INV-1/INV-2) and the write audit
(INV-6) hold by delegation; this handler only supplies the payload and renders
the result.

Governance: ``risk_tier=T1``, ``read_only=False``. T1 bypasses the approval gate
(the runner only gates T2+), but the runner still enforces the per-run allow-list
and records the ``tool_invocations`` row + ``tool.invoked``/``tool.result`` audit
for the call — so ``write_file`` is *not* in the default ad-hoc allow-list (it is
not read-only), and enabling it is a deliberate allow-list decision.

**Failure is a result, never a crash** (issue #207 §7): a disallowed content-type
or an over-cap payload comes back from the service as a typed ``ValidationError``,
which the handler folds into an ``ok=False`` :class:`ToolHandlerResult` (the model
reads it and can retry with a smaller/allowed file); a malformed base64 body is
the same. **Read-only test mode** (F-AB-5): when ``ctx.simulate_writes`` is set the
bytes are built and the arguments validated, but nothing is persisted — the write
is simulated so the harness never mutates state.

Format scope: this tool ships the text formats it can build losslessly from a
string — ``md`` / ``csv`` / ``json`` / ``txt`` / ``html`` (utf-8) — plus any
binary format the caller supplies as base64 (e.g. ``png``), gated by the artifact
allowlist in config. Rich generated formats (xlsx/docx/pptx via a library) are a
deliberate follow-up: no such dependency ships today, and the base64 path already
lets a code-sandbox caller (F-PY-2 #231) hand pre-built bytes to this same seam.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from app.core.errors import ValidationError
from app.domain.entities import ArtifactProducedBy
from app.domain.tools import ERROR_BAD_ARGS, ERROR_TOOL_ERROR, RiskTier, ToolHandlerResult
from app.services.artifacts_service import ArtifactLinks
from app.services.tools.types import ToolContext, ToolDefinition

#: The content-type an unspecified text write defaults to (utf-8 plain text).
_DEFAULT_CONTENT_TYPE = "text/plain"
#: How the ``content`` string is interpreted before it becomes bytes.
_ENCODING_TEXT = "text"
_ENCODING_BASE64 = "base64"
_ENCODINGS = (_ENCODING_TEXT, _ENCODING_BASE64)
#: The relative path the artifact-content route (F-TOOL-FILE-UI, #222) will serve.
#: Surfaced in the payload as a stable download reference the UI can resolve; the
#: route itself is a follow-up, so this is a reference, not a live URL.
_DOWNLOAD_REF_TEMPLATE = "/v1/artifacts/{artifact_id}/content"


def _build_bytes(content: str, encoding: str) -> bytes:
    """Turn the model-supplied ``content`` into bytes for the given ``encoding``.

    Text is utf-8 encoded directly; base64 is decoded strictly (a malformed body
    raises, which the handler maps to a typed ``ok=False`` — not a crash).
    """
    if encoding == _ENCODING_BASE64:
        return base64.b64decode(content, validate=True)
    return content.encode("utf-8")


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    """Build the bytes and persist them as an artifact (issue #220 AC-1/AC-2/AC-4)."""
    filename = str(args.get("filename") or "").strip()
    if not filename:
        return ToolHandlerResult(
            content="No filename provided.",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="no filename",
        )
    content = args.get("content")
    if not isinstance(content, str):
        return ToolHandlerResult(
            content="The 'content' argument must be a string (text, or base64 for binary).",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="no content",
        )
    content_type = str(args.get("content_type") or _DEFAULT_CONTENT_TYPE).strip()
    encoding = str(args.get("encoding") or _ENCODING_TEXT).strip().lower()
    if encoding not in _ENCODINGS:
        return ToolHandlerResult(
            content=f"Unsupported encoding {encoding!r}; use 'text' or 'base64'.",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="bad encoding",
        )

    # Build the bytes. A malformed base64 body is a tool-specific rejection the
    # model can recover from (issue #207 §7), not a crashed stream.
    try:
        data = _build_bytes(content, encoding)
    except (binascii.Error, ValueError):
        return ToolHandlerResult(
            content="The 'content' is not valid base64. Provide raw base64 with encoding='base64'.",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="bad base64",
        )

    # Read-only test mode (F-AB-5): the write is *simulated* — build + would-persist,
    # but nothing touches storage or the artifacts table (the negative for AC-4).
    if ctx.simulate_writes:
        return ToolHandlerResult(
            content=(
                f"[simulated] Would write {filename!r} ({content_type}, {len(data)} bytes). "
                "Read-only test mode: no artifact was persisted."
            ),
            summary=f"simulated write: {filename}",
            payload={
                "simulated": True,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
            },
        )

    # A run that offers no write seam cannot persist — report it as a typed result
    # rather than crashing (defensive: an enabled write_file is always wired with a
    # writer, but the allow-list and the seam are configured independently).
    if ctx.artifacts is None:
        return ToolHandlerResult(
            content="File writing is not available in this session.",
            ok=False,
            error=ERROR_TOOL_ERROR,
            summary="no artifact writer",
        )

    # Persist through CC-B. The service is already scoped to the caller's tenant +
    # owner (from the principal, never request input), enforces the allowlist/size
    # cap, and audits ``artifact.created`` (INV-1/INV-2/INV-6 by delegation). An
    # over-cap / disallowed-type payload is a typed ValidationError → ok=False.
    try:
        artifact = await ctx.artifacts.create_artifact(
            data=data,
            filename=filename,
            content_type=content_type,
            produced_by=ArtifactProducedBy.TOOL,
            links=ArtifactLinks(session_id=ctx.session_id),
        )
    except ValidationError as exc:
        reason = exc.detail or "the content-type or size is not allowed"
        return ToolHandlerResult(
            content=(
                f"The file could not be written: {reason}. "
                "Try a smaller file or an allowed content-type."
            ),
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="rejected",
        )

    download_ref = _DOWNLOAD_REF_TEMPLATE.format(artifact_id=artifact.id)
    return ToolHandlerResult(
        content=(
            f"Wrote {artifact.filename} ({artifact.mime_type}, {artifact.size_bytes} bytes). "
            f"Artifact id {artifact.id}; downloadable at {download_ref}."
        ),
        summary=f"wrote {artifact.filename}",
        payload={
            "artifact_id": str(artifact.id),
            "filename": artifact.filename,
            "content_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
            "download_ref": download_ref,
        },
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="write_file",
        description=(
            "Write a file (report, CSV, JSON, Markdown, HTML, or a binary such as "
            "PNG) as a durable, downloadable artifact — use this to produce the "
            "actual deliverable, not just describe it. Provide the 'content' as "
            "text, or as base64 with encoding='base64' for a binary format. "
            "Returns the artifact id and a download reference."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "The file name for the artifact, e.g. 'summary.md'.",
                },
                "content_type": {
                    "type": "string",
                    "description": (
                        "The MIME type of the file, e.g. 'text/markdown', 'text/csv', "
                        "'application/json', 'text/html', 'text/plain', or 'image/png'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The file contents: utf-8 text, or base64 when "
                        "encoding='base64' (for a binary format)."
                    ),
                },
                "encoding": {
                    "type": "string",
                    "enum": list(_ENCODINGS),
                    "description": (
                        "How 'content' is encoded: 'text' (default, utf-8) or "
                        "'base64' for binary."
                    ),
                },
            },
            "required": ["filename", "content_type", "content"],
        },
        handler=_write_file,
        risk_tier=RiskTier.T1,
        read_only=False,
    ),
)


__all__ = ["TOOLS"]
