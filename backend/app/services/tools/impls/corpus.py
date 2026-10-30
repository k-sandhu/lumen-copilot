"""Task-shaped corpus tools, using current permission checks for every read."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.domain.tools import ERROR_BAD_ARGS, ERROR_NOT_FOUND, RiskTier, ToolHandlerResult
from app.services.tools.handles import EvidenceHandles
from app.services.tools.impls.retrieval import rendered_evidence
from app.services.tools.types import ToolContext, ToolDefinition


def _ids(value: object, scope: list[UUID] | None) -> list[UUID] | None:
    if value is None:
        return scope
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Use at most 50 UUIDs in each scope filter.")
    ids = [UUID(str(item)) for item in value]
    return ids if scope is None else [item for item in ids if item in scope]


def _document_identity(value: str, ctx: ToolContext) -> UUID | None:
    """A current-conversation D handle or a UUID; navigation identity, never a grant."""
    document_id = ctx.handles.document_id(value) if ctx.handles else None
    if document_id is not None:
        return document_id
    try:
        return UUID(value)
    except ValueError:
        return None


def _unavailable_documents(values: list[str], ctx: ToolContext) -> str:
    shown = [repr(value if len(value) <= 40 else value[:40] + "…") for value in values[:3]]
    if len(values) > 3:
        shown.append(f"{len(values) - 3} more")
    listed = ", ".join(shown[:-1]) + (" and " if len(shown) > 1 else "") + shown[-1]
    returns = ", which find_documents returns," if "find_documents" in ctx.allowed_tools else ","
    return (
        f"document_ids has {'values that name' if len(values) > 1 else 'a value that names'} "
        f"no available document: {listed}. document_ids takes D handles shown in this "
        f"conversation (for example D2){returns} or omit it to search every permitted document."
    )


async def _document_ids(value: object, ctx: ToolContext) -> list[UUID] | None:
    """Resolve identities under permissions and collection/source scope, then narrow documents.

    Every value must name a document the principal can read now. A filename, an S
    handle, another conversation's handle, an unknown UUID and a revoked document
    all get the same field-specific error, so it discloses nothing (INV-2).
    """
    if value is None:
        return ctx.document_ids
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Use at most 50 values in each scope filter.")
    resolved = {str(item): _document_identity(str(item), ctx) for item in value}
    wanted = list(dict.fromkeys(item for item in resolved.values() if item is not None))
    scope = {}
    if ctx.collection_ids is not None:
        scope["collection_ids"] = ctx.collection_ids
    if ctx.source_ids is not None:
        scope["source_ids"] = ctx.source_ids
    permitted = (
        await ctx.retrieval.permitted_document_names(
            principal=ctx.principal, document_ids=wanted, **scope
        )
        if wanted
        else {}
    )
    unavailable = [raw for raw, item in resolved.items() if item is None or item not in permitted]
    if unavailable:
        raise ValidationError(_unavailable_documents(unavailable, ctx))
    return wanted if ctx.document_ids is None else [d for d in wanted if d in ctx.document_ids]


def _date(value: object) -> datetime | None:
    if value is None:
        return None
    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Use timestamps with a timezone.")
    return date


async def _filters(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "collection_ids": _ids(args.get("collection_ids"), ctx.collection_ids),
        "document_ids": await _document_ids(args.get("document_ids"), ctx),
        "source": args.get("source"),
        "mime_type": args.get("mime_type"),
        "created_after": _date(args.get("created_after")),
        "created_before": _date(args.get("created_before")),
        "modified_after": _date(args.get("modified_after")),
        "modified_before": _date(args.get("modified_before")),
    }
    # Assistant source scope (#581): not a model-supplied filter — the run's
    # published ``sourceIds`` narrow every corpus read. Passed only when set so
    # an ad-hoc context calls retrieval exactly as before.
    if ctx.source_ids is not None:
        filters["source_ids"] = ctx.source_ids
    return filters


def _bad_args(detail: str | None = None) -> ToolHandlerResult:
    return ToolHandlerResult(
        source_document_ids=(),
        ok=False,
        error=ERROR_BAD_ARGS,
        summary="invalid corpus arguments",
        content=detail
        or (
            "Check the stated ranges, UUID filters, timezone dates and cursor; retry with "
            "valid arguments."
        ),
    )


def _handle_unavailable(ctx: ToolContext) -> str:
    """Say what ``handle`` takes; identical for missing, foreign and revoked (INV-2)."""
    shown_by = " or ".join(
        name for name in ("search_passages", "find_documents") if name in ctx.allowed_tools
    )
    source = f"shown by {shown_by}" if shown_by else "shown earlier in this conversation"
    return (
        "No document or passage is available for that handle in this conversation and "
        "scope. handle takes a D handle (for example D2) or an S handle (for example S3) "
        f"{source}. If a passage may have changed, search again."
    )


async def _find(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        filters = await _filters(args, ctx)
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
        source_document_ids=(),
        content="\n\n".join(lines)
        or "No permitted documents match these filters. Try a shorter title or remove a filter.",
        summary=f"{len(page.items)} documents",
        hit_count=len(page.items),
        document_ids=tuple(item.document_id for item in page.items),
        payload={"next_cursor": page.next_cursor},
    )


async def _search(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        filters = await _filters(args, ctx)
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
                        source_document_ids=(),
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
        filtered = any(args.get(key) is not None for key in _FILTERS)
        if filters["document_ids"] == [] or filters["collection_ids"] == []:
            passages = []
            if filtered:
                return ToolHandlerResult(
                    source_document_ids=(),
                    content=(
                        "No permitted documents in this conversation's scope match these "
                        "filters. Remove or change a filter."
                    ),
                    summary="0 passages",
                    hit_count=0,
                )
        else:
            passages = await ctx.retrieval.search_text(
                principal=ctx.principal,
                query=query,
                k=min(int(args.get("k") or ctx.default_k), 20, ctx.max_k),
                collection_ids=filters["collection_ids"],
                document_ids=filters["document_ids"],
                **({"source_ids": filters["source_ids"]} if "source_ids" in filters else {}),
            )
    except ValidationError as exc:
        return _bad_args(exc.detail)
    except (ValueError, TypeError):
        return _bad_args()
    blocks = []
    for passage in passages:
        label = ctx.handles.passage(passage) if ctx.handles else str(passage.chunk_id)
        document = (
            ctx.handles.document(passage.document_id) if ctx.handles else str(passage.document_id)
        )
        blocks.append(rendered_evidence(passage, label, document=document))
    return ToolHandlerResult(
        source_document_ids=(),
        content="\n\n".join(blocks)
        or (
            "No matching permitted passages in the filtered documents. Try a broader query "
            "or remove a filter."
            if filtered
            else "No matching permitted passages. Try a short broader query or find_documents "
            "by title."
        ),
        summary=f"{len(passages)} passages",
        hit_count=len(passages),
        passages=tuple(passages),
        document_ids=tuple(dict.fromkeys(p.document_id for p in passages)),
    )


async def _read(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    try:
        handle = str(args.get("handle") or "").strip()
        start = int(args.get("start") or 0)
        end = int(args["end"]) if args.get("end") is not None else None
        scopes: dict[str, Any] = {
            "collection_ids": _ids(args.get("collection_ids"), ctx.collection_ids),
            "document_ids": await _document_ids(args.get("document_ids"), ctx),
        }
        if ctx.source_ids is not None:
            scopes["source_ids"] = ctx.source_ids
        if re.fullmatch(r"S[1-9][0-9]*", handle):
            # An S handle opens the text around that passage; any range is ignored.
            entry = ctx.handles.resolve(handle) if ctx.handles else None
            if entry is None or entry.get("kind") != "passage":
                raise LookupError
            chunk = UUID(str(entry["chunk_id"]))
            fresh = await ctx.retrieval.read_passages(
                principal=ctx.principal, chunk_ids=[chunk], **scopes
            )
            # Compare the complete identity, including media provenance, in an
            # isolated book so a stale read cannot allocate conversation handles.
            if not fresh or EvidenceHandles(existing={handle: entry}).passage(fresh[0]) != handle:
                raise LookupError
            document_id = UUID(str(entry["document_id"]))
            start = max(0, fresh[0].char_start - 1200)
            end = fresh[0].char_end + 1200
        elif re.fullmatch(r"D[1-9][0-9]*", handle):
            resolved = ctx.handles.document_id(handle) if ctx.handles else None
            if resolved is None:
                raise LookupError
            document_id = resolved
        else:
            try:
                document_id = UUID(handle)
            except ValueError:
                # Filenames get the same reply as unknown handles (INV-2).
                raise LookupError from None
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
            source_document_ids=(),
            ok=False,
            error=ERROR_NOT_FOUND,
            hit_count=0,
            content=_handle_unavailable(ctx),
            summary="document unavailable",
        )
    blocks = []
    for passage in page.passages:
        label = ctx.handles.passage(passage) if ctx.handles else str(passage.chunk_id)
        blocks.append(rendered_evidence(passage, label))
    range_text = (
        f"Available range: 0-{page.total_length}; "
        f"returned {page.returned_start}-{page.returned_end}."
    )
    if page.next_start is not None:
        document = ctx.handles.document(page.document_id) if ctx.handles else str(page.document_id)
        range_text += (
            f" Truncated: continue read_document(handle={document}, start={page.next_start}"
            f", end={end if end is not None else 'null'})."
        )
    return ToolHandlerResult(
        source_document_ids=(),
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
        "items": {"type": "string"},
        "description": (
            "Up to 50 D handles shown in this conversation (e.g. D2) or document UUIDs, "
            "intersected with pinned scope; omitted keeps its scope."
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
            "Each hit shows its S handle, its document's D handle and its character "
            "range, which read_document accepts. Cite S handles, never navigation IDs."
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
            "Open a handle from this conversation: a D handle reads its document's "
            "complete passages in a character range; an S handle reads the text around "
            "that passage. Use returned continuation to read omitted passages. Every "
            "read rechecks permissions."
        ),
        {
            "handle": {
                "type": "string",
                "description": (
                    "A D handle (document) or S handle (passage) shown in this "
                    "conversation, e.g. D2 or S5; a document UUID also works. An S "
                    "handle reads 1200 characters either side of the passage."
                ),
            },
            "start": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Inclusive character start for a D handle, default 0; full chunks may "
                    "begin earlier. Ignored for S handles."
                ),
            },
            "end": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Exclusive character end greater than start for a D handle; omitted "
                    "reads to document end. Ignored for S handles."
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
        ["handle"],
        _read,
    ),
)
