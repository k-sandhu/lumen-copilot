"""The retrieval tools the chat runtime exposes to the model (CC-6 #24).

Bridges a model-emitted :class:`~app.domain.llm.ToolCall` to the #45
``retrieval/`` service methods, named exactly as the WS ``ChatToolCall``
vocabulary (``search_text`` / ``search_documents`` / ``get_document``). It:

* declares the tool **specs** (:data:`TOOL_SPECS`) the gateway advertises to the
  model — the JSON-schema arguments the model fills in;
* **runs** a tool call against the permission-filtered retrieval service
  (:func:`run_tool`), returning a :class:`ToolOutcome` that carries both the
  text the model reads back *and* the permitted passages (for citations).

The permission filter lives **inside** ``retrieval/`` (INV-2); this module only
maps the model's arguments onto the right method and renders the results — it
never reaches a chunk by id the principal could not retrieve. A blank/foreign
query simply returns nothing, fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.auth.principal import Principal
from app.domain.llm import ToolCall, ToolSpec
from app.domain.retrieval import RetrievedPassage
from app.retrieval import RetrievalService

# Cap on the per-call ``k`` the model may request (keeps a hostile/large value
# from turning into a big scan — the service also clamps).
_MAX_K = 20
# How much passage text to surface back to the model per hit (keeps the tool
# result compact; the citation still carries the full snippet from the chunk).
_SNIPPET_BUDGET = 600


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_text",
        description=(
            "Hybrid semantic + keyword search over the user's own document "
            "passages. Use this to find evidence for the question. Returns ranked "
            "passages with their source document and a snippet. Search again with "
            "a refined query if results are thin."
        ),
        parameters={
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
    ),
    ToolSpec(
        name="search_documents",
        description=(
            "Find the user's documents by filename or metadata. Use this to "
            "locate a specific document before reading it with get_document."
        ),
        parameters={
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
    ),
    ToolSpec(
        name="get_document",
        description=(
            "Fetch the full text of one of the user's documents by id (from "
            "search_documents). Returns nothing if the document is not the "
            "user's."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The document id to fetch.",
                }
            },
            "required": ["document_id"],
        },
    ),
)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The result of running one tool call.

    ``content`` is the text fed back to the model as the tool's reply.
    ``passages`` are the **permitted** retrieved passages (empty for a
    document-level or fetch tool) the runtime turns into citations (INV-3).
    ``hit_count`` / ``document_ids`` feed the WS ``tool_result`` event + audit.
    """

    content: str
    hit_count: int
    passages: tuple[RetrievedPassage, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    summary: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


async def run_tool(
    retrieval: RetrievalService,
    *,
    principal: Principal,
    call: ToolCall,
    collection_ids: list[UUID] | None,
    default_k: int,
) -> ToolOutcome:
    """Dispatch ``call`` to the matching permission-filtered retrieval method.

    An unknown tool name, or malformed arguments, yields an empty/​error outcome
    the model can recover from (it never crashes the loop). All three paths run
    *inside* ``retrieval/`` and so are permission-filtered (INV-2) — a call as
    user A never reaches user B's data.
    """
    if call.name == "search_text":
        return await _run_search_text(
            retrieval, principal=principal, call=call, collection_ids=collection_ids, k=default_k
        )
    if call.name == "search_documents":
        return await _run_search_documents(retrieval, principal=principal, call=call)
    if call.name == "get_document":
        return await _run_get_document(retrieval, principal=principal, call=call)
    return ToolOutcome(content=f"Unknown tool: {call.name}", hit_count=0)


def _clamp_k(value: object, default: int) -> int:
    if not isinstance(value, int | float | str):
        return default
    try:
        k = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(_MAX_K, k))


async def _run_search_text(
    retrieval: RetrievalService,
    *,
    principal: Principal,
    call: ToolCall,
    collection_ids: list[UUID] | None,
    k: int,
) -> ToolOutcome:
    query = str(call.arguments.get("query") or "").strip()
    if not query:
        return ToolOutcome(content="No query provided.", hit_count=0, summary="no query")
    k = _clamp_k(call.arguments.get("k"), k)
    passages = await retrieval.search_text(
        principal=principal, query=query, k=k, collection_ids=collection_ids
    )
    if not passages:
        return ToolOutcome(
            content="No matching passages were found in your documents.",
            hit_count=0,
            summary="0 passages",
        )
    content = _render_passages(passages)
    return ToolOutcome(
        content=content,
        hit_count=len(passages),
        passages=tuple(passages),
        document_ids=tuple({p.document_id for p in passages}),
        summary=f"{len(passages)} passage(s)",
    )


async def _run_search_documents(
    retrieval: RetrievalService, *, principal: Principal, call: ToolCall
) -> ToolOutcome:
    name_or_query = str(call.arguments.get("name_or_query") or "").strip()
    if not name_or_query:
        return ToolOutcome(content="No document query provided.", hit_count=0, summary="no query")
    k = _clamp_k(call.arguments.get("k"), 10)
    matches = await retrieval.search_documents(
        principal=principal, name_or_query=name_or_query, k=k
    )
    if not matches:
        return ToolOutcome(content="No matching documents.", hit_count=0, summary="0 documents")
    lines = [f"- {m.document_name} (id: {m.document_id})" for m in matches]
    return ToolOutcome(
        content="Documents:\n" + "\n".join(lines),
        hit_count=len(matches),
        document_ids=tuple(m.document_id for m in matches),
        summary=f"{len(matches)} document(s)",
    )


async def _run_get_document(
    retrieval: RetrievalService, *, principal: Principal, call: ToolCall
) -> ToolOutcome:
    raw_id = str(call.arguments.get("document_id") or "").strip()
    try:
        document_id = UUID(raw_id)
    except ValueError:
        return ToolOutcome(content="Invalid document id.", hit_count=0, summary="invalid id")
    doc = await retrieval.get_document(principal=principal, document_id=document_id)
    if doc is None:
        # Existence non-disclosure (INV-2): a foreign/missing doc is "not found".
        return ToolOutcome(content="Document not found.", hit_count=0, summary="not found")
    body = doc.text[: _SNIPPET_BUDGET * 4]
    return ToolOutcome(
        content=f"Document: {doc.document_name}\n\n{body}",
        hit_count=1,
        document_ids=(doc.document_id,),
        summary=doc.document_name,
    )


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


__all__ = ["TOOL_SPECS", "ToolOutcome", "run_tool"]
