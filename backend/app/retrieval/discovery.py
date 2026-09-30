"""Bounded, permission-first relational discovery and complete-chunk navigation."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.db import models
from app.domain.discovery import DiscoveryDocument, DocumentPage, DocumentRead
from app.domain.retrieval import RetrievedPassage
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import _document_permitted


async def find_documents(
    session: AsyncSession,
    *,
    allow: AllowSet,
    query: str = "",
    limit: int = 10,
    cursor: str | None = None,
    sort: str = "title_asc",
    source: str | None = None,
    mime_type: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    collection_ids: Sequence[UUID] | None = None,
    document_ids: Sequence[UUID] | None = None,
) -> DocumentPage:
    if not 1 <= limit <= 50 or sort not in {
        "title_asc",
        "title_desc",
        "created_asc",
        "created_desc",
        "modified_asc",
        "modified_desc",
    }:
        raise ValidationError("Use limit 1–50 and a documented title/created/modified sort.")
    if created_after and created_before and created_after >= created_before:
        raise ValidationError("created_after must precede created_before.")
    if modified_after and modified_before and modified_after >= modified_before:
        raise ValidationError("modified_after must precede modified_before.")
    binding = hashlib.sha256(
        json.dumps(
            [
                str(allow.tenant_id),
                str(allow.grant_principal_id),
                query,
                sort,
                source,
                mime_type,
                str(created_after),
                str(created_before),
                str(modified_after),
                str(modified_before),
                sorted(map(str, collection_ids)) if collection_ids is not None else None,
                sorted(map(str, document_ids)) if document_ids is not None else None,
            ]
        ).encode()
    ).hexdigest()
    title = func.lower(func.coalesce(models.Document.title, models.Document.filename))
    is_date = sort.startswith(("created", "modified"))
    order = (
        models.Document.source_modified_at
        if sort.startswith("modified")
        else models.Document.created_at
        if sort.startswith("created")
        else title
    )
    descending = sort.endswith("desc")
    stmt = (
        select(models.Document, models.Source.type)
        .outerjoin(
            models.Source,
            and_(
                models.Source.id == models.Document.source_id,
                models.Source.tenant_id == allow.tenant_id,
            ),
        )
        .where(models.Document.tenant_id == allow.tenant_id, _document_permitted(allow))
    )
    if query:
        stmt = stmt.where(
            or_(
                *[
                    cast(column, String).icontains(query, autoescape=True)
                    for column in (
                        models.Document.title,
                        models.Document.filename,
                        models.Document.source_path,
                        models.Document.discovery_metadata,
                    )
                ]
            )
        )
    if source is not None:
        stmt = stmt.where(func.coalesce(models.Source.type, "upload") == source)
    if mime_type is not None:
        stmt = stmt.where(models.Document.mime_type == mime_type)
    if created_after is not None:
        stmt = stmt.where(models.Document.created_at >= created_after)
    if created_before is not None:
        stmt = stmt.where(models.Document.created_at < created_before)
    if modified_after is not None:
        stmt = stmt.where(models.Document.source_modified_at >= modified_after)
    if modified_before is not None:
        stmt = stmt.where(models.Document.source_modified_at < modified_before)
    if collection_ids is not None:
        stmt = stmt.where(models.Document.collection_id.in_(collection_ids))
    if document_ids is not None:
        stmt = stmt.where(models.Document.id.in_(document_ids))
    if cursor is not None:
        try:
            if len(cursor) > 4096:
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(value, dict) or value.get("binding") != binding:
                raise ValueError
            key = value["key"]
            if key is None and not sort.startswith("modified"):
                raise ValueError
            position: datetime | str | None = (
                None if key is None else datetime.fromisoformat(str(key)) if is_date else str(key)
            )
            document_id = UUID(value["id"])
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValidationError(
                "Invalid cursor; restart discovery with the same filters."
            ) from exc
        if position is None:
            stmt = stmt.where(order.is_(None), models.Document.id > document_id)
        else:
            comparison = order < position if descending else order > position
            stmt = stmt.where(
                or_(
                    comparison,
                    and_(order == position, models.Document.id > document_id),
                    order.is_(None),
                )
            )
    stmt = stmt.order_by(
        (order.desc() if descending else order.asc()).nulls_last(), models.Document.id
    ).limit(limit + 1)
    rows = (await session.execute(stmt)).all()
    items = tuple(
        DiscoveryDocument(
            document_id=row.id,
            title=row.title or row.filename,
            filename=row.filename,
            source_path=row.source_path,
            source=source_type or "upload",
            mime_type=row.mime_type,
            created_at=row.created_at,
            metadata=row.discovery_metadata or {},
            source_modified_at=row.source_modified_at,
        )
        for row, source_type in rows[:limit]
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        date = last.source_modified_at if sort.startswith("modified") else last.created_at
        key = (
            (date.isoformat() if date is not None else None)
            if is_date
            else (last.title or last.filename).lower()
        )
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"binding": binding, "key": key, "id": str(last.id)}).encode()
        ).decode()
    return DocumentPage(items, next_cursor)


async def read_document(
    session: AsyncSession,
    *,
    allow: AllowSet,
    document_id: UUID,
    start: int = 0,
    end: int | None = None,
    max_passages: int = 5,
    collection_ids: Sequence[UUID] | None = None,
    document_ids: Sequence[UUID] | None = None,
) -> DocumentRead | None:
    if start < 0 or (end is not None and end <= start) or not 1 <= max_passages <= 20:
        raise ValidationError("Use start >= 0, end > start and max_passages 1–20.")
    permitted = select(models.Document).where(
        models.Document.tenant_id == allow.tenant_id,
        models.Document.id == document_id,
        _document_permitted(allow),
    )
    if collection_ids is not None:
        permitted = permitted.where(models.Document.collection_id.in_(collection_ids))
    if document_ids is not None:
        permitted = permitted.where(models.Document.id.in_(document_ids))
    document = (await session.execute(permitted)).scalar_one_or_none()
    if document is None:
        return None
    # Repeat the current permission predicate in the chunk read, including tenant.
    chunks = (
        select(models.Chunk)
        .join(models.Document)
        .where(
            models.Chunk.tenant_id == allow.tenant_id,
            models.Document.tenant_id == allow.tenant_id,
            models.Chunk.document_id == document_id,
            _document_permitted(allow),
        )
    )
    if collection_ids is not None:
        chunks = chunks.where(models.Document.collection_id.in_(collection_ids))
    if document_ids is not None:
        chunks = chunks.where(models.Document.id.in_(document_ids))
    total = (
        await session.execute(
            select(func.max(models.Chunk.char_end))
            .join(models.Document)
            .where(
                models.Chunk.tenant_id == allow.tenant_id,
                models.Chunk.document_id == document_id,
                models.Document.tenant_id == allow.tenant_id,
                _document_permitted(allow),
            )
        )
    ).scalar_one() or 0
    chunks = chunks.where(models.Chunk.char_end > start)
    if end is not None:
        chunks = chunks.where(models.Chunk.char_start < end)
    rows = (
        (
            await session.execute(
                chunks.order_by(models.Chunk.char_end, models.Chunk.ord, models.Chunk.id).limit(
                    max_passages + 1
                )
            )
        )
        .scalars()
        .all()
    )
    passages = tuple(
        RetrievedPassage(
            chunk_id=row.id,
            document_id=document_id,
            document_name=document.filename,
            ord=row.ord,
            text=row.text,
            char_start=row.char_start,
            char_end=row.char_end,
            score=0,
        )
        for row in rows[:max_passages]
    )
    returned_start = min((p.char_start for p in passages), default=start)
    returned_end = max((p.char_end for p in passages), default=start)
    if len(rows) > max_passages and rows[max_passages].char_end <= returned_end:
        raise ValidationError(
            "Overlapping passages share a range boundary; increase max_passages "
            "(at most 20) or narrow the range. No continuation has skipped evidence."
        )
    return DocumentRead(
        document_id,
        document.filename,
        passages,
        total,
        returned_start,
        returned_end,
        returned_end if len(rows) > max_passages else None,
    )
