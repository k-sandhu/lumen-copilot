"""Search route — GET /search (permission-trimmed, cited) (#83).

Contract-first: the shapes match ``contracts/openapi.yaml`` exactly
(``SearchResponse`` = ``query`` + ``results`` + ``hidden_count`` +
optional ``direct_answer`` + ``next_cursor``; ``SearchResult`` /
``SearchCitation`` / ``DirectAnswer`` / ``MatchSpan``). The router validates in →
calls **one** service (:class:`~app.services.search_service.SearchService`) →
shapes out (ADR-0004); all orchestration — the permissioned retrieval through the
#45 chokepoint, the optional grounded answer, the audit emit, the cursor codec —
lives in the service. This layer only (de)serialises and threads correlation
context.

**Auto-discovered (ADR-0008 §3).** This module exposes a module-level ``router``
and is included by ``api/v1/__init__.py``'s package scan — there is no edit to any
aggregator.

**Permissioned + audited (mission filters #1/#4).** Every route requires the
bearer token (contract ``bearerAuth``); a missing/invalid token is a 401 (INV-4)
via ``current_user`` before any retrieval runs. The tenant + ownership allow-set
is derived from the resolved principal inside ``retrieval/`` (INV-1/INV-2), so the
results contain only passages the caller may see; the search emits one
``retrieval.query`` audit event (INV-6). A malformed cursor is a 422 (INV-8) from
the service's cursor codec.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
    get_llm_gateway,
)
from app.llm import LLMGateway
from app.retrieval import RetrievalService
from app.services.search_service import (
    DirectAnswerData,
    SearchCitationData,
    SearchPage,
    SearchResultData,
    SearchService,
)

router = APIRouter(tags=["search"])


# --- Adapter injectables (overridable in tests) ----------------------------


def get_llm_gateway_dep() -> LLMGateway:
    """The #36 LLM gateway as a FastAPI dependency (delegates to the cached singleton).

    A thin injectable wrapper so the router obtains the one model caller by
    injection (and the offline API tests override it via ``dependency_overrides``
    with a no-op/scripted gateway, keeping the direct-answer path testable without
    a provider key). The gateway remains the single model caller (ADR-0004).
    """
    return get_llm_gateway()


LLMGatewayDep = Annotated[LLMGateway, Depends(get_llm_gateway_dep)]


def get_retrieval_service(session: DbSession, gateway: LLMGatewayDep) -> RetrievalService:
    """The #45 retrieval chokepoint as a FastAPI dependency (the only retrieval path).

    Built over this request's session + the gateway (it embeds the query). Exposed
    as an injectable so the offline API tests override it with a fake whose
    ``search`` does not need pgvector — the hybrid pgvector/full-text path is the
    retrieval adapter's own (live) contract, exercised in ``test_search_service``'s
    live test. The permission filter lives *inside* this service (INV-1/INV-2);
    overriding it in a test still routes results through the same domain type.
    """
    return RetrievalService(session, gateway=gateway)


RetrievalDep = Annotated[RetrievalService, Depends(get_retrieval_service)]


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class MatchSpan(BaseModel):
    """``#/components/schemas/MatchSpan`` — a matched span within the snippet."""

    model_config = {"extra": "forbid"}

    start: int
    end: int


class SearchResult(BaseModel):
    """``#/components/schemas/SearchResult`` — one permission-trimmed ranked result."""

    model_config = {"extra": "forbid"}

    id: UUID
    title: str
    snippet: str
    match_spans: list[MatchSpan]
    why_matched: str
    source: str
    type: str
    permission: str
    last_indexed: datetime
    owner: UUID | None = None
    document_id: UUID | None = None
    document_kind: str | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    score: float | None = None


class SearchCitation(BaseModel):
    """``#/components/schemas/SearchCitation`` — a citation backing the direct answer."""

    model_config = {"extra": "forbid"}

    result_id: UUID
    snippet: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None


class DirectAnswer(BaseModel):
    """``#/components/schemas/DirectAnswer`` — the optional cited synthesized answer."""

    model_config = {"extra": "forbid"}

    text: str
    citations: list[SearchCitation]


class SearchResponse(BaseModel):
    """``#/components/schemas/SearchResponse`` — a page of permission-trimmed results."""

    model_config = {"extra": "forbid"}

    query: str
    results: list[SearchResult]
    hidden_count: int
    direct_answer: DirectAnswer | None = None
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_result(result: SearchResultData) -> SearchResult:
    return SearchResult(
        id=result.id,
        title=result.title,
        snippet=result.snippet,
        match_spans=[MatchSpan(start=s.start, end=s.end) for s in result.match_spans],
        why_matched=result.why_matched,
        source=result.source,
        type=result.type,
        permission=result.permission,
        last_indexed=result.last_indexed,
        owner=result.owner,
        document_id=result.document_id,
        document_kind=result.document_kind,
        time_start_ms=result.time_start_ms,
        time_end_ms=result.time_end_ms,
        transcript_segment_id=result.transcript_segment_id,
        speaker_id=result.speaker_id,
        speaker_name=result.speaker_name,
        score=result.score,
    )


def _to_citation(citation: SearchCitationData) -> SearchCitation:
    return SearchCitation(
        result_id=citation.result_id,
        snippet=citation.snippet,
        char_start=citation.char_start,
        char_end=citation.char_end,
        time_start_ms=citation.time_start_ms,
        time_end_ms=citation.time_end_ms,
        transcript_segment_id=citation.transcript_segment_id,
        speaker_id=citation.speaker_id,
        speaker_name=citation.speaker_name,
    )


def _to_direct_answer(answer: DirectAnswerData | None) -> DirectAnswer | None:
    if answer is None:
        return None
    return DirectAnswer(
        text=answer.text,
        citations=[_to_citation(c) for c in answer.citations],
    )


def _to_response(page: SearchPage) -> SearchResponse:
    return SearchResponse(
        query=page.query,
        results=[_to_result(r) for r in page.results],
        hidden_count=page.hidden_count,
        direct_answer=_to_direct_answer(page.direct_answer),
        next_cursor=page.next_cursor,
    )


# --- Routes -----------------------------------------------------------------


@router.get("/search", response_model=SearchResponse, response_model_exclude_none=True)
async def search(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    gateway: LLMGatewayDep,
    retrieval: RetrievalDep,
    q: Annotated[str, Query(min_length=1, description="The search query.")],
    collection_id: Annotated[UUID | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,  # noqa: A002 — contract param name
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResponse:
    """Permission-trimmed ranked search, with an optional cited answer.

    Runs ``q`` through the ``retrieval/`` chokepoint: the ranked ``results``
    contain ONLY passages in the caller's effective allow-set (INV-2) within their
    tenant (INV-1). A ``direct_answer`` is included when the permitted results
    support it (every claim cited, INV-3) and omitted otherwise. ``hidden_count``
    discloses how many otherwise-matching results were withheld by permissions
    without leaking their content. The search emits a ``retrieval.query`` audit
    event (INV-6). Requires a valid bearer token (unauthenticated → 401, INV-4);
    a malformed cursor → 422 (INV-8).
    """
    service = SearchService(
        session,
        principal=principal,
        gateway=gateway,
        audit=make_audit_sink(tenant_id),
        retrieval=retrieval,
        # The audit envelope requires a non-empty request_id / source_ip (spec
        # 0004 §2.4); fall back to a sentinel when the client supplied neither.
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    page = await service.search(
        query=q,
        collection_id=collection_id,
        source=source,
        content_type=type,
        cursor=cursor,
        limit=limit,
    )
    await session.commit()
    return _to_response(page)
