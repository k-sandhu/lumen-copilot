"""Search typeahead + recent-history routes (spec 0005, epic #144).

Contract-first: shapes match ``contracts/openapi.yaml`` (``SuggestResponse`` /
``Suggestion``; ``RecentSearchList`` / ``RecentSearch``). Auto-discovered
(ADR-0008 §3) — a module-level ``router`` the package scan includes; no aggregator
edit. The router validates in → calls **one** service → shapes out (ADR-0004).

* ``GET /search/suggest`` — permission-trimmed typeahead. Document suggestions run
  through the same ``retrieval/`` chokepoint as ``/search`` (INV-2): a title the
  caller can't open is never suggested, and the lookup emits one ``retrieval.query``
  audit event so it is provable after the fact (INV-6, spec 0005 §5). An
  all-whitespace ``q`` is **422** (the ``\\S`` pattern, INV-8).
* ``GET``/``DELETE /search/recent`` — the caller's own recent queries; per-user
  (one user's recents never appear for another, INV-2).

Every route requires the bearer token (INV-4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
)
from app.api.v1.search import RetrievalDep
from app.services.suggest_service import SuggestionData, SuggestService

router = APIRouter(tags=["search"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class Suggestion(BaseModel):
    """``#/components/schemas/Suggestion``."""

    model_config = {"extra": "forbid"}

    kind: Literal["completion", "document", "saved_search"]
    text: str
    document_id: UUID | None = None
    source: str | None = None
    saved_search_id: UUID | None = None


class SuggestResponse(BaseModel):
    """``#/components/schemas/SuggestResponse``."""

    model_config = {"extra": "forbid"}

    suggestions: list[Suggestion]


class RecentSearchResponse(BaseModel):
    """``#/components/schemas/RecentSearch``."""

    model_config = {"extra": "forbid"}

    query: str
    last_used_at: datetime


class RecentSearchListResponse(BaseModel):
    """``#/components/schemas/RecentSearchList``."""

    model_config = {"extra": "forbid"}

    items: list[RecentSearchResponse]


def _to_suggestion(data: SuggestionData) -> Suggestion:
    return Suggestion(
        kind=data.kind,
        text=data.text,
        document_id=data.document_id,
        source=data.source,
        saved_search_id=data.saved_search_id,
    )


@router.get("/search/suggest", response_model=SuggestResponse, response_model_exclude_none=True)
async def suggest_search(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    retrieval: RetrievalDep,
    make_audit_sink: AuditSinkFactory,
    q: Annotated[str, Query(min_length=1, pattern=r"\S", description="The partial query.")],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> SuggestResponse:
    """Permission-trimmed typeahead for ``q``; emits one retrieval audit event (INV-6)."""
    service = SuggestService(
        session,
        principal=principal,
        retrieval=retrieval,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    suggestions = await service.suggest(q=q, limit=limit)
    # The audit row flushed within this request transaction — commit it.
    await session.commit()
    return SuggestResponse(suggestions=[_to_suggestion(s) for s in suggestions])


@router.get("/search/recent", response_model=RecentSearchListResponse)
async def list_recent_searches(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    retrieval: RetrievalDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> RecentSearchListResponse:
    """The caller's recent search queries, newest first."""
    service = SuggestService(session, principal=principal, retrieval=retrieval)
    recents = await service.list_recent(limit=limit)
    return RecentSearchListResponse(
        items=[RecentSearchResponse(query=r.query, last_used_at=r.last_used_at) for r in recents]
    )


@router.delete("/search/recent", status_code=status.HTTP_204_NO_CONTENT)
async def clear_recent_searches(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    retrieval: RetrievalDep,
) -> Response:
    """Clear the caller's recent search history (idempotent)."""
    service = SuggestService(session, principal=principal, retrieval=retrieval)
    await service.clear_recent()
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
