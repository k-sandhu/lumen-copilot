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


def _format_player_time(milliseconds: int) -> str:
    """Render an integer player offset without losing millisecond precision."""
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _media_provenance(passage: RetrievedPassage) -> str:
    """Model-visible speaker/time label for a valid media passage."""
    if passage.time_start_ms is None or passage.time_end_ms is None:
        return ""
    speaker = passage.speaker_name or passage.speaker_id
    identity = (
        f", {speaker} ({passage.speaker_id})"
        if passage.speaker_name and passage.speaker_id
        else (f", {speaker}" if speaker else "")
    )
    return (
        f", time {_format_player_time(passage.time_start_ms)}-"
        f"{_format_player_time(passage.time_end_ms)}{identity}"
    )


def rendered_snippet(text: str, budget: int = _SNIPPET_BUDGET) -> str:
    """Complete model-visible passage, also retained verbatim by compaction.

    ``budget`` remains accepted for callers using the legacy signature. Evidence
    is bounded by passage count and context fitting, never by cutting a passage
    in half. The runtime uses this same helper for compaction protection.
    """
    return text.strip()


def rendered_evidence(passage: RetrievedPassage, handle: str) -> str:
    """Handle-labelled complete evidence with the source's player provenance."""
    return (
        f"[{handle}] {passage.document_name}{_media_provenance(passage)}\n"
        f"{rendered_snippet(passage.text)}"
    )


def _clamp_k(value: object, default: int, *, maximum: int = _MAX_K) -> int:
    # An omitted/invalid ``k`` falls back to ``default`` — but still clamped to the
    # ceiling (#424 re-review): under a tight budget ``maximum`` can be lower than
    # the tool's own default, and the default must not slip past it either.
    if not isinstance(value, int | float | str):
        return max(1, min(maximum, default))
    try:
        k = int(value)
    except (TypeError, ValueError):
        return max(1, min(maximum, default))
    return max(1, min(maximum, k))


def _render_passages(
    passages: list[RetrievedPassage], snippet_budget: int = _SNIPPET_BUDGET
) -> str:
    """Render retrieved passages as the tool reply the model reads.

    Each passage is labelled with its source document + chunk id so the model can
    attribute a claim. Complete passages keep late facts visible; count ceilings
    and the context engine bound the input cost before each model call.
    """
    blocks: list[str] = []
    for i, p in enumerate(passages, start=1):
        snippet = rendered_snippet(p.text, snippet_budget)
        blocks.append(
            f"[{i}] {p.document_name} (document id: {p.document_id}, chunk {p.chunk_id}, "
            f"chars {p.char_start}-{p.char_end}"
            f"{_media_provenance(p)}):\n"
            f"{snippet}"
        )
    return "\n\n".join(blocks)


def _document_not_found_content(ctx: ToolContext) -> str:
    """The one non-disclosing reply for a handle that resolves to nothing (#758).

    Missing, malformed, another owner's and another tenant's handles all get
    this identical corrective message (INV-2) — it says what ``document_id``
    accepts and where to get an id, naming only tools in ``ctx.allowed_tools``
    (as the truncation-recovery text does), and discloses no length or range.
    """
    allowed = ctx.allowed_tools
    accepted: list[str] = []
    if "search_text" in allowed:
        accepted.append("the 'document id:' value from search_text results")
    id_tools = [name for name in ("search_documents", "list_documents") if name in allowed]
    if id_tools:
        accepted.append("an id from " + " or ".join(id_tools))
    if not accepted:
        accepted.append("a document id")
    accepted.extend(("a chunk id shown in search results", "an exact filename as displayed"))
    content = (
        "Document not found. No accessible document matches that document_id. "
        "document_id takes " + ", ".join(accepted[:-1]) + ", or " + accepted[-1] + "."
    )
    if "search_documents" in allowed:
        content += " To find a document by filename, call search_documents."
    return content


async def _search_text(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolHandlerResult(
            source_document_ids=(), content="No query provided.", summary="no query"
        )
    # Clamp to the budget-derived ceiling, not just the tool's absolute _MAX_K
    # (#424 review, finding 3): under a tight context window ``ctx.max_k`` is
    # lowered by the assembler, so even an explicit large ``k`` cannot pull in
    # more evidence than the remaining budget. ``ctx.max_k`` defaults to _MAX_K,
    # so a roomy run is unchanged.
    k = _clamp_k(args.get("k"), ctx.default_k, maximum=min(_MAX_K, ctx.max_k))
    passages = await ctx.retrieval.search_text(
        principal=ctx.principal,
        query=query,
        k=k,
        collection_ids=ctx.collection_ids,
        document_ids=ctx.document_ids,
    )
    if not passages:
        return ToolHandlerResult(
            source_document_ids=(),
            content="No matching passages were found in your documents.",
            summary="0 passages",
        )
    document_ids = tuple({p.document_id for p in passages})
    return ToolHandlerResult(
        source_document_ids=(),
        content=_render_passages(passages, ctx.snippet_budget),
        summary=f"{len(passages)} passage(s)",
        hit_count=len(passages),
        passages=tuple(passages),
        document_ids=document_ids,
        payload={"hit_count": len(passages), "document_ids": [str(d) for d in document_ids]},
    )


async def _search_documents(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    name_or_query = str(args.get("name_or_query") or "").strip()
    if not name_or_query:
        return ToolHandlerResult(
            source_document_ids=(), content="No document query provided.", summary="no query"
        )
    # Clamp to the budget-derived ceiling like search_text (#424 review, finding
    # 2): a tight window lowers ``ctx.max_k`` so even an explicit large ``k`` is
    # bounded. Defaults to _MAX_K, so a roomy run is unchanged.
    k = _clamp_k(args.get("k"), 10, maximum=min(_MAX_K, ctx.max_k))
    matches = await ctx.retrieval.search_documents(
        principal=ctx.principal, name_or_query=name_or_query, k=k
    )
    if not matches:
        return ToolHandlerResult(
            source_document_ids=(), content="No matching documents.", summary="0 documents"
        )
    lines = [f"- {m.document_name} (id: {m.document_id})" for m in matches]
    document_ids = tuple(m.document_id for m in matches)
    return ToolHandlerResult(
        source_document_ids=(),
        content="Documents:\n" + "\n".join(lines),
        summary=f"{len(matches)} document(s)",
        hit_count=len(matches),
        document_ids=document_ids,
        payload={"document_ids": [str(d) for d in document_ids]},
    )


async def _list_documents(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    # Clamp to the budget-derived ceiling as well as the tool's own _LIST_MAX
    # (#424 final re-review): a tight window lowers ``ctx.max_k`` so the listing
    # degrades to a budgeted count instead of enumerating 50 then refusing on the
    # next turn. ``ctx.max_k`` defaults to _LIST_MAX (50), so a roomy run is
    # unchanged — it still lists up to 50.
    k = _clamp_k(args.get("k"), _LIST_MAX, maximum=min(_LIST_MAX, ctx.max_k))
    matches = await ctx.retrieval.list_documents(principal=ctx.principal, k=k)
    if not matches:
        # A clean "nothing here" is an ok result, not an error — the user simply
        # has no documents of their own and none shared with them yet.
        return ToolHandlerResult(
            source_document_ids=(),
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
        source_document_ids=(),
        content=content,
        summary=f"{len(matches)} document(s)",
        hit_count=len(matches),
        document_ids=document_ids,
        payload={"document_ids": [str(d) for d in document_ids]},
    )


async def _get_document(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    raw_id = str(args.get("document_id") or "").strip()
    if raw_id.startswith("D") and ctx.handles is not None:
        entry = ctx.handles.resolve(raw_id)
        raw_id = str(entry["document_id"]) if entry and entry.get("kind") == "document" else ""
    resolved = ctx.document_id_resolver(raw_id) if ctx.document_id_resolver else None
    if resolved is not None:
        raw_id = str(resolved)
    # Resolution (document id → chunk id → exact name, with tolerant parsing)
    # lives inside ``retrieval/`` under the same owner-or-grant predicate
    # (#758); duck-typed test fakes that predate it keep the UUID-only path.
    resolver = getattr(ctx.retrieval, "resolve_document_text", None)
    if resolver is not None:
        resolution = await resolver(principal=ctx.principal, handle=raw_id)
        if resolution.candidates:
            lines = [f"- {c.document_name} (id: {c.document_id})" for c in resolution.candidates]
            return ToolHandlerResult(
                source_document_ids=(),
                content=(
                    "Several accessible documents have that exact name. "
                    "Retry with one document id:\n" + "\n".join(lines)
                ),
                ok=False,
                error=ERROR_BAD_ARGS,
                summary="ambiguous name",
                payload={"document_ids": [str(c.document_id) for c in resolution.candidates]},
            )
        doc = resolution.document
    else:
        try:
            document_id = UUID(raw_id.strip())
        except ValueError:
            doc = None
        else:
            doc = await ctx.retrieval.get_document(principal=ctx.principal, document_id=document_id)
    if doc is None:
        # Existence non-disclosure (INV-2): missing, malformed, foreign and
        # cross-tenant handles share this one corrective outcome (#758).
        return ToolHandlerResult(
            source_document_ids=(),
            content=_document_not_found_content(ctx),
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="not found",
        )
    # Bound the returned body to the budget-derived snippet allowance (#424 third
    # re-review): a document read is ~4× a passage snippet, and a tight context
    # window lowers ``ctx.snippet_budget`` so ``get_document`` doesn't blow the
    # remaining budget either. Capped at the tool's own ceiling; roomy = unchanged.
    body_budget = max(1, min(ctx.snippet_budget, _SNIPPET_BUDGET)) * 4
    body = doc.text[:body_budget]
    total_length = len(doc.text)
    truncated = len(body) < total_length
    content = (
        f"Document: {doc.document_name}\n"
        f"Returned range: 0-{len(body)} of {total_length} characters (end-exclusive).\n\n"
        f"{body}"
    )
    if truncated:
        recovery = (
            "use search_text with a targeted query to retrieve evidence from the remaining text."
            if "search_text" in ctx.allowed_tools
            else (
                "This assistant cannot inspect the omitted text with its available tools. "
                "Ask the user to supply the relevant excerpt or switch to an authorized "
                "search-capable assistant."
            )
        )
        content += (
            f"\n\n[Truncated: characters {len(body)}-{total_length} are not shown. "
            f"This legacy tool reads only a prefix; {recovery}]"
        )
    return ToolHandlerResult(
        source_document_ids=(),
        content=content,
        summary=doc.document_name,
        hit_count=1,
        document_ids=(doc.document_id,),
        payload={
            "document_id": str(doc.document_id),
            "returned_range": [0, len(body)],
            "total_length": total_length,
            "truncated": truncated,
        },
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="search_text",
        default_offered=False,
        description=(
            "Hybrid semantic + keyword search over the documents the user can "
            "access (their own and any shared with them). Use this to find "
            "evidence for the question. Returns ranked passages with their source "
            "document and complete passage text; each passage header shows its "
            "document id (as 'document id: ...'), usable with get_document. "
            "Count limits and the context "
            "engine bound evidence cost. If no "
            "passages match, broaden the query, use search_documents to locate a "
            "document by filename, or list_documents to see available documents."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The natural-language search query.",
                },
                "k": {
                    "type": "integer",
                    "description": (
                        "How many passages to return (1-20; defaults to 6, lowered "
                        "when context is tight)."
                    ),
                    "minimum": 1,
                    "maximum": _MAX_K,
                    "default": 6,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_search_text,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="search_documents",
        default_offered=False,
        description=(
            "Find accessible documents by filename substring. This search matches "
            "filenames only, not document body text or other metadata. Use the "
            "returned id with get_document to read a capped text prefix. If there "
            "are no matches, try a shorter filename term or list_documents."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "name_or_query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Non-empty filename text to match (substring search).",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_K,
                    "description": "Maximum documents to return (1-20; defaults to 10).",
                    "default": 10,
                },
            },
            "required": ["name_or_query"],
            "additionalProperties": False,
        },
        handler=_search_documents,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="list_documents",
        default_offered=False,
        description=(
            "List the documents the user can access — their own uploads and any "
            "shared with them — without needing a search term. Use this to answer "
            "questions like 'what documents do I have access to' or to show the "
            "user what is available. Returns document names with their ids (each "
            "usable with get_document). The list is capped; to find one specific "
            "document by filename, use search_documents instead. An empty result "
            "means no accessible documents are available; narrow a full list with "
            "search_documents or use search_text to look for evidence."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "k": {
                    "type": "integer",
                    "description": "Maximum number of documents to list (1-50; defaults to 50).",
                    "minimum": 1,
                    "maximum": _LIST_MAX,
                    "default": _LIST_MAX,
                },
            },
            # No required args — enumeration needs no query.
            "additionalProperties": False,
        },
        handler=_list_documents,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
    ToolDefinition(
        name="get_document",
        default_offered=False,
        description=(
            "Read a prefix of an accessible document. document_id accepts the "
            "'document id:' value from search_text results, an id returned by "
            "search_documents or list_documents, a chunk id shown by search_text, "
            "or an exact filename as displayed. Use this for an overview, not to "
            "conclude that unseen text contains no answer. Returns up to 2,400 "
            "characters, less under a tight context budget, with the returned range "
            "and total length. A truncated read explicitly marks omitted text; use "
            "search_text when available with a targeted query for evidence beyond "
            "the prefix. Otherwise ask the user to supply the relevant excerpt "
            "or switch to an authorized search-capable assistant. "
            "Missing and inaccessible documents both return not found."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": (
                        "The document to fetch: a 'document id:' value from "
                        "search_text, an id from search_documents or "
                        "list_documents, a chunk id shown by search_text, or an "
                        "exact filename as displayed."
                    ),
                }
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        handler=_get_document,
        risk_tier=RiskTier.T0,
        read_only=True,
    ),
)


__all__ = ["TOOLS", "rendered_snippet"]
