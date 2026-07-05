"""The retrieval tools, behind the governed registry (CC-7 #207 §1).

The read-only retrieval tools the chat agent uses to ground answers in the user's
own corpus: ``search_text`` (hybrid passage search), ``search_documents`` (find a
document by name), ``get_document`` (fetch one document's text) — the trio the
agent has had since #24 — plus ``list_documents`` (enumerate what the user can
access, no query — #371, the answer to "what documents do I have access to"). All
are expressed as :class:`~app.services.tools.types.ToolDefinition`s the registry
auto-discovers. Each handler is a **thin adapter** that delegates to the
permission-filtered ``retrieval/`` service (INV-2 lives *inside* ``retrieval/``;
this module only maps the model's args onto the right method and renders the reply).

**ADR-0010 (done, #192/#193):** ``search_text`` runs on OpenSearch (the single
retrieval store); ``search_documents`` / ``list_documents`` / ``get_document`` stay
relational — but all share the identical owner-or-grant permission predicate inside
``retrieval/`` (a document owned by the caller *or* granted to them, directly or
via its collection), so the agent surface never disagrees about what the caller may
see. These handlers call the service's public methods only — one registry, no fork
of the tool layer. The owner-or-grant parity is proven live in ``test_grants.py``.

All are **T0, read-only, no approval** (spec 0004 §2.5 — the entire MVP is T0), so
they bypass the approval gate; the runner still enforces the allow-list + audits +
records the ``tool_invocations`` row for each.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.retrieval import RetrievedPassage
from app.domain.tools import ERROR_BAD_ARGS, RiskTier, ToolHandlerResult
from app.services.tools.types import ToolContext, ToolDefinition

# Cap on the per-call ``k`` the model may request (keeps a hostile/large value
# from turning into a big scan — the service also clamps). Mirrors the old
# ``chat_tools._MAX_K`` so tool behavior is unchanged across the migration.
_MAX_K = 20
# Cap on how many documents a single ``list_documents`` enumeration returns (kept
# in step with ``retrieval.service._MAX_DOCUMENT_HITS``); larger corpora are
# reached by narrowing with ``search_documents`` (pagination is a fast-follow).
_LIST_MAX = 50
# How much passage text to surface back to the model per hit (keeps the tool
# result compact; the citation still carries the full snippet from the chunk).
_SNIPPET_BUDGET = 600


def _clamp_k(value: object, default: int, *, maximum: int = _MAX_K) -> int:
    if not isinstance(value, int | float | str):
        return default
    try:
        k = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, k))


def _render_passages(passages: list[RetrievedPassage]) -> str:
    """Render retrieved passages as the tool reply the model reads.

    Each passage is labelled with its source document + chunk id so the model can
    attribute a claim, and trimmed to a budget so the context stays bounded. The
    full snippet still travels to the citation via the passage object.
    """
    blocks: list[str] = []
    for i, p in enumerate(passages, start=1):
        snippet = p.text.strip()
        if len(snippet) > _SNIPPET_BUDGET:
            snippet = snippet[:_SNIPPET_BUDGET].rstrip() + "…"
        blocks.append(
            f"[{i}] {p.document_name} (chunk {p.chunk_id}, chars {p.char_start}-{p.char_end}):\n"
            f"{snippet}"
        )
    return "\n\n".join(blocks)


async def _search_text(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolHandlerResult(content="No query provided.", summary="no query")
    k = _clamp_k(args.get("k"), ctx.default_k)
    passages = await ctx.retrieval.search_text(
        principal=ctx.principal, query=query, k=k, collection_ids=ctx.collection_ids
    )
    if not passages:
        return ToolHandlerResult(
            content="No matching passages were found in your documents.",
            summary="0 passages",
        )
    document_ids = tuple({p.document_id for p in passages})
    return ToolHandlerResult(
        content=_render_passages(passages),
        summary=f"{len(passages)} passage(s)",
        hit_count=len(passages),
        passages=tuple(passages),
        document_ids=document_ids,
        payload={"hit_count": len(passages), "document_ids": [str(d) for d in document_ids]},
    )


async def _search_documents(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    name_or_query = str(args.get("name_or_query") or "").strip()
    if not name_or_query:
        return ToolHandlerResult(content="No document query provided.", summary="no query")
    k = _clamp_k(args.get("k"), 10)
    matches = await ctx.retrieval.search_documents(
        principal=ctx.principal, name_or_query=name_or_query, k=k
    )
    if not matches:
        return ToolHandlerResult(content="No matching documents.", summary="0 documents")
    lines = [f"- {m.document_name} (id: {m.document_id})" for m in matches]
    document_ids = tuple(m.document_id for m in matches)
    return ToolHandlerResult(
        content="Documents:\n" + "\n".join(lines),
        summary=f"{len(matches)} document(s)",
        hit_count=len(matches),
        document_ids=document_ids,
        payload={"document_ids": [str(d) for d in document_ids]},
    )


async def _list_documents(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    k = _clamp_k(args.get("k"), _LIST_MAX, maximum=_LIST_MAX)
    matches = await ctx.retrieval.list_documents(principal=ctx.principal, k=k)
    if not matches:
        # A clean "nothing here" is an ok result, not an error — the user simply
        # has no documents of their own and none shared with them yet.
        return ToolHandlerResult(
            content=(
                "You don't have access to any documents yet — nothing has been "
                "uploaded to your account or shared with you."
            ),
            summary="0 documents",
        )
    lines = [f"- {m.document_name} (id: {m.document_id})" for m in matches]
    document_ids = tuple(m.document_id for m in matches)
    content = "Documents you can access:\n" + "\n".join(lines)
    # No silent truncation: when the result fills the requested limit there may be
    # more, so say so — the model then offers to narrow rather than implying this is
    # the whole set. Keyed off the effective limit ``k`` (not the hard ``_LIST_MAX``)
    # so the hint still fires when the model asks for fewer than the cap. A false
    # positive at exactly ``k`` accessible documents is acceptable.
    if len(matches) >= k:
        content += (
            f"\n\n(Showing the first {k}; there may be more — narrow with "
            "search_documents using a filename or keyword.)"
        )
    return ToolHandlerResult(
        content=content,
        summary=f"{len(matches)} document(s)",
        hit_count=len(matches),
        document_ids=document_ids,
        payload={"document_ids": [str(d) for d in document_ids]},
    )


async def _get_document(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    raw_id = str(args.get("document_id") or "").strip()
    try:
        document_id = UUID(raw_id)
    except ValueError:
        # A malformed id is a tool-specific rejection, not a crash — the runner
        # passes the ``ok=False`` through and the run continues (issue #207 §7).
        return ToolHandlerResult(
            content="Invalid document id.", ok=False, error=ERROR_BAD_ARGS, summary="invalid id"
        )
    doc = await ctx.retrieval.get_document(principal=ctx.principal, document_id=document_id)
    if doc is None:
        # Existence non-disclosure (INV-2): a foreign/missing doc is "not found".
        return ToolHandlerResult(content="Document not found.", summary="not found")
    body = doc.text[: _SNIPPET_BUDGET * 4]
    return ToolHandlerResult(
        content=f"Document: {doc.document_name}\n\n{body}",
        summary=doc.document_name,
        hit_count=1,
        document_ids=(doc.document_id,),
        payload={"document_id": str(doc.document_id)},
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="search_text",
        description=(
            "Hybrid semantic + keyword search over the documents the user can "
            "access (their own and any shared with them). Use this to find "
            "evidence for the question. Returns ranked passages with their source "
            "document and a snippet. Search again with a refined query if results "
            "are thin."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The natural-language search query.",
                },
                "k": {
                    "type": "integer",
                    "description": "How many passages to return (1-20).",
                    "minimum": 1,
                    "maximum": _MAX_K,
                },
            },
            "required": ["query"],
        },
        handler=_search_text,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="search_documents",
        description=(
            "Find documents the user can access (their own or shared with them) "
            "by filename or metadata. Use this to locate a specific document "
            "before reading it with get_document."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "name_or_query": {
                    "type": "string",
                    "description": "Filename or keyword to match documents by.",
                },
                "k": {"type": "integer", "minimum": 1, "maximum": _MAX_K},
            },
            "required": ["name_or_query"],
        },
        handler=_search_documents,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="list_documents",
        description=(
            "List the documents the user can access — their own uploads and any "
            "shared with them — without needing a search term. Use this to answer "
            "questions like 'what documents do I have access to' or to show the "
            "user what is available. Returns document names with their ids (each "
            "usable with get_document). The list is capped; to find one specific "
            "document by name, use search_documents instead."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "k": {
                    "type": "integer",
                    "description": "Maximum number of documents to list (1-50).",
                    "minimum": 1,
                    "maximum": _LIST_MAX,
                },
            },
            # No required args — enumeration needs no query.
        },
        handler=_list_documents,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="get_document",
        description=(
            "Fetch the full text of a document the user can access by id (from "
            "search_documents). Returns nothing if the document is not one the "
            "user can access."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The document id to fetch.",
                }
            },
            "required": ["document_id"],
        },
        handler=_get_document,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
)


__all__ = ["TOOLS"]
