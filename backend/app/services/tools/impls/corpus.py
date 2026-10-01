"""Task-shaped corpus tools, using current permission checks for every read."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.domain.tools import ERROR_BAD_ARGS, ERROR_NOT_FOUND, RiskTier, ToolHandlerResult
from app.services.tools.handles import EvidenceHandles
from app.services.tools.types import ToolContext, ToolDefinition


def _ids(value: object, scope: list[UUID] | None) -> list[UUID] | None:
    if value is None:
        return scope
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Use at most 50 UUIDs in each scope filter.")
    ids = [UUID(str(item)) for item in value]
    return ids if scope is None else [item for item in ids if item in scope]


def _date(value: object) -> datetime | None:
    if value is None:
        return None
    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Use timestamps with a timezone.")
    return date


def _filters(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {
        "collection_ids": _ids(args.get("collection_ids"), ctx.collection_ids),
        "document_ids": _ids(args.get("document_ids"), ctx.document_ids),
        "source": args.get("source"),
        "mime_type": args.get("mime_type"),
        "created_after": _date(args.get("created_after")),
        "created_before": _date(args.get("created_before")),
        "modified_after": _date(args.get("modified_after")),
        "modified_before": _date(args.get("modified_before")),
    }


def _bad_args(detail: str | None = None) -> ToolHandlerResult:
    return ToolHandlerResult(
        ok=False,
        error=ERROR_BAD_ARGS,
        summary="invalid corpus arguments",
        content=detail
        or (
            "Check the stated ranges, UUID filters, timezone dates and cursor; retry with "
            "valid arguments."
        ),
    )


async def _find(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        filters = _filters(args, ctx)
        page = await ctx.retrieval.find_documents(
            principal=ctx.principal,
            query=str(args.get("query") or "").strip(),
            limit=min(int(args.get("limit") or 10), ctx.max_k),
            cursor=args.get("cursor"),
            sort=args.get("sort") or "title_asc",
            **filters,
        )
    except ValidationError as exc:
        return _bad_args(exc.detail)
    except (ValueError, TypeError):
        return _bad_args()
    lines = []
    for item in page.items:
        label = ctx.handles.document(item.document_id) if ctx.handles else str(item.document_id)
        lines.append(
            f"[{label}] {item.title}\nFilename: {item.filename}; source: {item.source}; "
            f"MIME: {item.mime_type}; path: {item.source_path or '(none)'}; "
            f"modified: {getattr(item, 'source_modified_at', None) or '(unknown)'}"
        )
    if page.next_cursor:
        lines.append(
            f"More documents available. Continue find_documents with cursor: {page.next_cursor}"
        )
    return ToolHandlerResult(
        content="\n\n".join(lines)
        or "No permitted documents match these filters. Try a shorter title or remove a filter.",
        summary=f"{len(page.items)} documents",
        hit_count=len(page.items),
        document_ids=tuple(item.document_id for item in page.items),
        payload={"next_cursor": page.next_cursor},
    )


async def _search(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        filters = _filters(args, ctx)
        if any(
            filters[key] is not None
            for key in (
                "source",
                "mime_type",
                "created_after",
                "created_before",
                "modified_after",
                "modified_before",
            )
        ):
            eligible: list[UUID] = []
            cursor = None
            while True:
                page = await ctx.retrieval.find_documents(
                    principal=ctx.principal, limit=50, cursor=cursor, **filters
                )
                eligible.extend(item.document_id for item in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
                if len(eligible) >= 1000:
                    return ToolHandlerResult(
                        ok=False,
                        error=ERROR_BAD_ARGS,
                        content=(
                            "Too many documents match. Narrow source, MIME, dates or "
                            "document scope."
                        ),
                        summary="narrow document filters",
                    )
            filters["document_ids"] = eligible
        query = str(args.get("query") or "").strip()
        if not query:
            return _bad_args()
        if filters["document_ids"] == [] or filters["collection_ids"] == []:
            passages = []
        else:
            passages = await ctx.retrieval.search_text(
                principal=ctx.principal,
                query=query,
                k=min(int(args.get("k") or ctx.default_k), 20, ctx.max_k),
                collection_ids=filters["collection_ids"],
                document_ids=filters["document_ids"],
            )
    except ValidationError as exc:
        return _bad_args(exc.detail)
    except (ValueError, TypeError):
        return _bad_args()
    blocks = []
    for passage in passages:
        label = ctx.handles.passage(passage) if ctx.handles else str(passage.chunk_id)
        blocks.append(
            f"[{label}] {passage.document_name} "
            f"(chars {passage.char_start}-{passage.char_end})\n{passage.text}"
        )
    return ToolHandlerResult(
        content="\n\n".join(blocks)
        or "No matching permitted passages. Try a short broader query or find_documents by title.",
        summary=f"{len(passages)} passages",
        hit_count=len(passages),
        passages=tuple(passages),
        document_ids=tuple(dict.fromkeys(p.document_id for p in passages)),
    )


async def _read(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        identifier = args.get("document")
        around = args.get("around")
        if bool(identifier) == bool(around):
            return _bad_args()
        start = int(args.get("start") or 0)
        end = int(args["end"]) if args.get("end") is not None else None
        scopes = {
            "collection_ids": _ids(args.get("collection_ids"), ctx.collection_ids),
            "document_ids": _ids(args.get("document_ids"), ctx.document_ids),
        }
        if around:
            entry = ctx.handles.resolve(str(around)) if ctx.handles else None
            if entry is None or entry.get("kind") != "passage":
                raise LookupError
            chunk = UUID(str(entry["chunk_id"]))
            fresh = await ctx.retrieval.read_passages(
                principal=ctx.principal, chunk_ids=[chunk], **scopes
            )
            # Compare the complete identity, including media provenance, in an
            # isolated book so a stale read cannot allocate conversation handles.
            if (
                not fresh
                or EvidenceHandles(existing={str(around): entry}).passage(fresh[0]) != around
            ):
                raise LookupError
            identifier = entry["document_id"]
            start = max(0, fresh[0].char_start - 1200)
            end = fresh[0].char_end + 1200
        elif isinstance(identifier, str) and identifier.startswith("D"):
            entry = ctx.handles.resolve(identifier) if ctx.handles else None
            if entry is None or entry.get("kind") != "document":
                raise LookupError
            identifier = entry["document_id"]
        document_id = UUID(str(identifier))
        page = await ctx.retrieval.read_document(
            principal=ctx.principal,
            document_id=document_id,
            start=start,
            end=end,
            max_passages=min(int(args.get("max_passages") or 5), ctx.max_k),
            **scopes,
        )
    except ValidationError as exc:
        return _bad_args(exc.detail)
    except (ValueError, TypeError):
        return _bad_args()
    except (LookupError, KeyError):
        page = None
    if page is None:
        return ToolHandlerResult(
            ok=False,
            error=ERROR_NOT_FOUND,
            hit_count=0,
            content=(
                "Document or handle not available in this conversation and scope. " "Search again."
            ),
            summary="document unavailable",
        )
    blocks = []
    for passage in page.passages:
        label = ctx.handles.passage(passage) if ctx.handles else str(passage.chunk_id)
        blocks.append(f"[{label}] {page.document_name}\n{passage.text}")
    range_text = (
        f"Available range: 0-{page.total_length}; "
        f"returned {page.returned_start}-{page.returned_end}."
    )
    if page.next_start is not None:
        document = ctx.handles.document(page.document_id) if ctx.handles else str(page.document_id)
        range_text += (
            f" Truncated: continue read_document(document={document}, start={page.next_start}"
            f", end={end if end is not None else 'null'})."
        )
    return ToolHandlerResult(
        content="\n\n".join(blocks + [range_text]),
        summary=f"{len(page.passages)} complete passages",
        hit_count=len(page.passages),
        passages=tuple(page.passages),
        document_ids=(page.document_id,),
        payload={
            "total_length": page.total_length,
            "returned_range": [page.returned_start, page.returned_end],
            "next_start": page.next_start,
        },
    )


_FILTERS: dict[str, Any] = {
    "modified_after": {
        "type": "string",
        "description": "Inclusive source-modified timestamp with timezone; unknown dates excluded.",
    },
    "modified_before": {
        "type": "string",
        "description": "Exclusive source-modified timestamp with timezone; unknown dates excluded.",
    },
    "source": {
        "type": "string",
        "description": "Exact source kind, e.g. upload, web or gdrive; omitted means any.",
    },
    "mime_type": {"type": "string", "description": "Exact MIME type; omitted means any."},
    "created_after": {
        "type": "string",
        "description": "Inclusive creation timestamp with timezone; omitted means no lower bound.",
    },
    "created_before": {
        "type": "string",
        "description": "Exclusive creation timestamp with timezone; omitted means no upper bound.",
    },
    "collection_ids": {
        "type": "array",
        "maxItems": 50,
        "items": {"type": "string", "format": "uuid"},
        "description": (
            "Up to 50 collection UUIDs intersected with conversation scope; omitted "
            "keeps its scope."
        ),
    },
    "document_ids": {
        "type": "array",
        "maxItems": 50,
        "items": {"type": "string", "format": "uuid"},
        "description": (
            "Up to 50 document UUIDs intersected with pinned scope; omitted keeps " "its scope."
        ),
    },
}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str], handler: Any
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        json_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        risk_tier=RiskTier.T0,
        read_only=True,
        requires_approval=False,
        handler=handler,
    )


TOOLS = (
    _tool(
        "search_passages",
        (
            "Search relevant complete passages, then read around a passage for context. "
            "Cite S handles, never navigation IDs."
        ),
        {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "A short topical query; required.",
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Passage count 1–20; default 6, reduced when context is tight.",
            },
            **_FILTERS,
        },
        ["query"],
        _search,
    ),
    _tool(
        "find_documents",
        (
            "Find documents by real title, filename, source path or public metadata. "
            "Returns D navigation handles and a continuation cursor."
        ),
        {
            "query": {
                "type": "string",
                "description": "Literal substring; default empty lists accessible documents.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Page size 1–50; default 10, reduced by context budget.",
            },
            "cursor": {
                "type": "string",
                "maxLength": 4096,
                "description": (
                    "Returned continuation cursor; omit on the first page, keep "
                    "filters/sort unchanged."
                ),
            },
            "sort": {
                "type": "string",
                "enum": [
                    "title_asc",
                    "title_desc",
                    "created_asc",
                    "created_desc",
                    "modified_asc",
                    "modified_desc",
                ],
                "description": "Sort order; default title_asc; document UUID breaks ties.",
            },
            **_FILTERS,
        },
        [],
        _find,
    ),
    _tool(
        "read_document",
        (
            "Read complete passages in a character range or around S evidence; use "
            "returned continuation to read omitted passages. Every read rechecks "
            "permissions."
        ),
        {
            "document": {
                "type": "string",
                "description": (
                    "Document UUID or current conversation D handle; required unless "
                    "around is supplied."
                ),
            },
            "around": {
                "type": "string",
                "pattern": "^S[1-9][0-9]*$",
                "description": (
                    "Current conversation S handle; reads 1200 characters either "
                    "side, ignoring start/end. Supply instead of document."
                ),
            },
            "start": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Inclusive character start, default 0; full chunks may begin earlier."
                ),
            },
            "end": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Exclusive character end greater than start; omitted reads to " "document end."
                ),
            },
            "max_passages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Complete passages 1–20; default 5, reduced by context budget.",
            },
            "collection_ids": _FILTERS["collection_ids"],
            "document_ids": _FILTERS["document_ids"],
        },
        [],
        _read,
    ),
)
