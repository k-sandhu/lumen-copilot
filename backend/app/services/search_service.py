"""Permission-trimmed, cited grounded search — the ``GET /search`` use-case (#83).

The orchestration layer behind ``GET /search`` (ADR-0004: ``services/`` compose
adapters; the router calls exactly one service and shapes its result). It wraps
the existing ``retrieval/`` chokepoint (#45) — **the only** retrieval path, with
the INV-2 permission filter applied *inside* it — and turns the ranked,
permission-trimmed passages into the contract's ``SearchResponse`` shape:

* one :class:`SearchResultData` per permitted passage (title, snippet + matched
  spans, why-it-matched, owner, freshness/``last_indexed``, permission, score);
* a ``hidden_count`` of otherwise-matching results withheld by permissions;
* an **optional** cited :class:`DirectAnswerData` — a short synthesized answer
  whose every claim is backed by ``citations[]`` that reference result ids
  present in this page (spec 0004 INV-3); omitted when nothing permitted answers
  the query (mission filter #2: prefer "no answer" over an unsourced claim).

**Permissioned by default (INV-1/INV-2).** The service never issues an
unfiltered query. Every result comes from ``RetrievalService.search`` keyed off
the resolved :class:`~app.auth.principal.Principal`, whose allow-set is *same
tenant* + (*owned by the caller* **or** *explicitly granted to them* — directly
or via a cascading collection grant, spec 0004 §2.2). A cross-tenant passage, or
one the caller neither owns nor was granted, is never retrieved, so it can never
appear in ``results`` — the negative tests assert exactly this, and the
grant-positive test asserts a granted document's passages *do* appear.

**Hidden count (MVP).** Because the permission filter is *structural* — there is
no path that returns an unfiltered candidate set to diff against (ADR-0004, the
whole point of the single chokepoint) — the service cannot count passages it was
never allowed to see without violating the invariant. So ``hidden_count`` is
``0``: every otherwise-matching passage is either fully visible (owned or
granted) or outside the allow-set (another tenant, or neither owned nor granted)
and never retrieved. The field is wired through end-to-end so a future surface
that *can* say "N hidden by your permissions" populates it without any caller
change.

**Audit (mission filter #4 / INV-6).** Every search emits exactly one
``retrieval.query`` event through the one :class:`~app.services.audit.AuditSink`
(never the raw query — a non-reversible hash, spec 0004 §2.4), recording the
hit count and the document ids surfaced. The audit row flushes within the
request transaction.

**Cursor.** Retrieval returns a *ranked list*, not a keyset over a column, so the
page cursor is an opaque offset into that ranking (encoded so a client cannot
forge a meaningful one; a malformed cursor is rejected fail-closed → 422, INV-8).
The service over-fetches one extra result to decide whether a next page exists.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.errors import AppError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import DocumentRepository, RecentSearchRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.llm import ChatMessage, Role
from app.domain.retrieval import RetrievedPassage
from app.llm import LLMGateway
from app.retrieval import MAX_K, RetrievalService
from app.services.audit import AuditSink

log = get_logger(__name__)

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# A short, stable cursor prefix so a decoded payload is recognisably one of ours.
_CURSOR_PREFIX = "search:"

# How many results to consider for grounding the optional direct answer. Kept
# small so the prompt stays cheap and the answer cites only the strongest hits.
_DIRECT_ANSWER_TOP_K = 4

# The direct answer is a SHORT cited synthesis (the grounding prompt asks for a
# few sentences); bounding the generation keeps search latency and cost flat
# instead of letting a rambling model run to its own stop (#395). Generous
# relative to the asked-for length so truncation is not a realistic outcome.
_DIRECT_ANSWER_MAX_TOKENS = 300
# Hard ceiling on the snippet text fed into the grounding prompt per result, so a
# very long passage cannot blow the context window.
_GROUNDING_SNIPPET_CHARS = 600

# The fixed source/type for the MVP corpus: everything searchable today is an
# uploaded document (spec 0004 §2.2 — `chat`/`connector` sources are reserved).
_SOURCE_UPLOAD = "upload"
_TYPE_DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class MatchSpanData:
    """A character span within a result's snippet that matched the query."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SearchResultData:
    """One permission-trimmed ranked result (the contract ``SearchResult``).

    Always within the caller's tenant + allow-set (INV-1/INV-2) — the service
    only constructs these from passages ``retrieval/`` already permitted. The
    router serialises this into the wire model; the service holds no HTTP type.
    """

    id: UUID
    title: str
    snippet: str
    match_spans: list[MatchSpanData]
    why_matched: str
    source: str
    type: str
    permission: str
    last_indexed: datetime
    owner: UUID | None = None
    document_id: UUID | None = None
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SearchCitationData:
    """A citation backing the direct answer (the contract ``SearchCitation``).

    References a result present in this page by ``result_id`` so the answer can
    never cite a passage the caller could not retrieve (INV-3).
    """

    result_id: UUID
    snippet: str | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True, slots=True)
class DirectAnswerData:
    """An optional cited synthesized answer (the contract ``DirectAnswer``)."""

    text: str
    citations: list[SearchCitationData]


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of permission-trimmed ranked results (the contract ``SearchResponse``)."""

    query: str
    results: list[SearchResultData]
    hidden_count: int
    direct_answer: DirectAnswerData | None = None
    next_cursor: str | None = None


# --- Cursor codec (opaque; carries the rank offset of the next page) --------


def _encode_cursor(offset: int) -> str:
    """Encode the next-page rank offset as an opaque URL-safe cursor."""
    raw = f"{_CURSOR_PREFIX}{offset}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor back into a non-negative rank offset.

    Raises:
        ValidationError: the cursor is not one this server issued (malformed
            base64, missing prefix, or non-integer / negative payload). Fail-closed
            → 422 rather than silently returning the first page (INV-8).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    payload = raw[len(_CURSOR_PREFIX) :]
    if not payload.isdigit():
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    return int(payload)


def _clamp_limit(limit: int | None) -> int:
    """Clamp the requested page size into the contract's [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _hash_query(query: str) -> str:
    """A stable, non-reversible hash of the query for the audit trail (spec 0004 §2.4).

    The audit log records a query **hash**, not the raw text, so the trail does
    not duplicate potentially sensitive question text while still letting a
    reviewer correlate the retrieval event (matches the chat runtime's hashing).
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _term_pattern(query: str) -> re.Pattern[str] | None:
    """Build a case-insensitive alternation of the query's word tokens, or ``None``.

    Used to highlight which spans of a snippet matched (the contract's
    ``match_spans``). Tokens are escaped, so a query with regex metacharacters is
    matched literally. A query with no word tokens yields ``None`` (no spans).
    """
    tokens = [re.escape(t) for t in re.findall(r"\w+", query) if t]
    if not tokens:
        return None
    return re.compile("|".join(tokens), re.IGNORECASE)


def _match_spans(snippet: str, pattern: re.Pattern[str] | None) -> list[MatchSpanData]:
    """Find the non-overlapping spans of ``snippet`` matching the query terms."""
    if pattern is None:
        return []
    return [MatchSpanData(start=m.start(), end=m.end()) for m in pattern.finditer(snippet)]


class SearchService:
    """Permission-trimmed, cited grounded search (the ``GET /search`` use-case).

    Constructed per-request with the async session, the resolved principal, the
    #36 LLM gateway, and the one audit sink. It composes the #45
    :class:`RetrievalService` (the permission chokepoint), the tenant-scoped
    :class:`DocumentRepository` (for the per-result owner + freshness the
    retrieval domain type does not carry), and — for the optional direct answer —
    the gateway's grounded ``chat``. It returns the domain :class:`SearchPage`;
    the router maps it to the wire ``SearchResponse``.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        gateway: LLMGateway,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self._session = session
        self._principal = principal
        self._gateway = gateway
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        # The retrieval service is the chokepoint; injectable so the offline tests
        # supply a fake whose ``search`` does not need pgvector, defaulting to the
        # real adapter built over this request's session + gateway.
        self._retrieval = retrieval or RetrievalService(session, gateway=gateway)
        self._documents = DocumentRepository(session, principal.tenant_id)
        self._recent = RecentSearchRepository(session, principal.tenant_id)

    async def search(
        self,
        *,
        query: str,
        collection_id: UUID | None = None,
        source: str | None = None,
        content_type: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        with_direct_answer: bool = True,
    ) -> SearchPage:
        """Run one permission-trimmed, optionally-cited search.

        Retrieves the permitted ranked passages for ``query`` through the
        ``retrieval/`` chokepoint (INV-1/INV-2), pages them by the opaque rank
        cursor, enriches each with its owner + freshness, computes match spans,
        optionally grounds a cited direct answer on the top of the page, and emits
        the ``retrieval.query`` audit event (INV-6). ``source``/``content_type``
        narrow the (uniform, all-document) MVP corpus: a non-``upload`` source or a
        non-``document`` type matches nothing and yields an empty page (never an
        error, never another corpus).

        Args:
            query: the natural-language query (lexical + semantic; ranked).
            collection_id: optional narrowing to one of the caller's collections
                (still permission-filtered — a foreign collection contributes
                nothing).
            source: optional ``ResultSource`` narrowing; only ``upload`` matches
                the MVP corpus.
            content_type: optional content-type narrowing; only ``document``
                matches the MVP corpus.
            cursor: opaque rank cursor from a previous page's ``next_cursor``.
            limit: page size (clamped to the contract's [1, 100]).
            with_direct_answer: whether to attempt the optional cited answer.

        Returns:
            A :class:`SearchPage` of permitted results (best first), the optional
            cited answer, ``hidden_count``, and the next-page cursor (or ``None``).
        """
        page_size = _clamp_limit(limit)
        offset = _decode_cursor(cursor) if cursor else 0

        # Record the executed query in the caller's recent history (spec 0005) — a
        # de-duplicated, capped per-user list that powers the search typeahead.
        # After the cursor decode so a malformed cursor (422) records nothing;
        # blank queries are ignored by the repository. Commits with the search.
        await self._recent.record(self._principal.user_id, query)

        # A source/type the MVP corpus cannot satisfy short-circuits to an empty,
        # permission-trimmed page (still audited) — never another corpus.
        corpus_excluded = (source is not None and source != _SOURCE_UPLOAD) or (
            content_type is not None and content_type != _TYPE_DOCUMENT
        )

        passages: list[RetrievedPassage] = []
        if not corpus_excluded and offset < MAX_K:
            # Over-fetch the page window + 1 so we can tell whether a next page
            # exists; retrieval ranks at most ``MAX_K`` results, so cap the ask
            # there (retrieval clamps anyway) — the reachable band is [0, MAX_K).
            wanted = min(offset + page_size + 1, MAX_K)
            collection_ids = [collection_id] if collection_id is not None else None
            passages = await self._retrieval.search(
                principal=self._principal,
                query=query,
                k=wanted,
                collection_ids=collection_ids,
            )

        window_end = offset + page_size
        window = passages[offset:window_end]
        # A next page exists only if retrieval returned more than this window AND
        # the next window would still start inside the reachable band. Without
        # the ``< MAX_K`` guard the service could hand back a next_cursor whose
        # offset sits at the retrieval ceiling, yielding a silently-empty page
        # (#270). Results past rank MAX_K are unreachable by design; the cap is
        # explicit here rather than a silent truncation.
        has_next = len(passages) > window_end and window_end < MAX_K
        next_cursor = _encode_cursor(window_end) if has_next else None

        results = await self._to_results(query, window)
        direct_answer = (
            await self._direct_answer(query, results) if with_direct_answer and results else None
        )

        await self._audit_search(
            query=query,
            results=results,
            collection_id=collection_id,
            has_direct_answer=direct_answer is not None,
        )

        return SearchPage(
            query=query,
            results=results,
            # MVP: the structural permission filter excludes hidden passages before
            # they are ever retrieved, so there is no permitted-vs-hidden diff to
            # count without an unfiltered query (forbidden, ADR-0004). 0 today;
            # the grant-aware allow-set will populate it (spec 0004 §2.2).
            hidden_count=0,
            direct_answer=direct_answer,
            next_cursor=next_cursor,
        )

    # --- result projection --------------------------------------------------

    async def _to_results(
        self, query: str, passages: list[RetrievedPassage]
    ) -> list[SearchResultData]:
        """Project permitted passages into wire-ready results (owner + freshness enriched).

        The retrieval domain type carries the citation provenance (chunk/document
        ids, text, offsets, score) but not the owner or freshness; those come from
        the tenant-scoped :class:`DocumentRepository` (the only SQL), looked up
        per distinct source document. A document that cannot be re-read (deleted
        between retrieval and enrichment, or in another tenant) is dropped rather
        than surfaced with a guessed owner.

        Every passage here already cleared the ``retrieval/`` chokepoint, whose
        INV-2 predicate admits a document owned by the caller **or** explicitly
        granted to them (directly or via a cascading collection grant —
        ``_document_permitted``, spec 0004 §2.2). So enrichment must **not**
        re-narrow to strict ownership: a grant-visible (non-owned) document's
        passages are legitimately retrieved and must be projected too (matching
        what ``/search/suggest`` already surfaces). The only re-check kept here is
        the repository's tenant scope (INV-1); INV-2 stays the chokepoint's job.
        """
        pattern = _term_pattern(query)
        results: list[SearchResultData] = []
        # Cache the per-document lookups so repeated passages from one document
        # cost one query, not one per chunk.
        doc_cache: dict[UUID, tuple[UUID, datetime] | None] = {}
        for passage in passages:
            meta = doc_cache.get(passage.document_id)
            if passage.document_id not in doc_cache:
                document = await self._documents.get(passage.document_id)
                # The repository is tenant-scoped (INV-1), so a cross-tenant or
                # missing document yields no metadata and is dropped. Ownership is
                # NOT re-asserted: the chokepoint already permitted this passage by
                # ownership OR grant, and re-narrowing to ownership would wrongly
                # drop a grant-visible document (the INV-2 authority is the
                # chokepoint, not this enrichment step).
                meta = (
                    (document.owner_id, document.updated_at) if document is not None else None
                )
                doc_cache[passage.document_id] = meta
            if meta is None:
                continue
            owner_id, last_indexed = meta
            snippet = passage.text
            results.append(
                SearchResultData(
                    # The result id is the chunk id — the unit a citation resolves
                    # to (INV-3), and what `document_id` clicks through from.
                    id=passage.chunk_id,
                    title=passage.document_name,
                    snippet=snippet,
                    match_spans=_match_spans(snippet, pattern),
                    why_matched=_why_matched(snippet, pattern),
                    source=_SOURCE_UPLOAD,
                    type=_TYPE_DOCUMENT,
                    permission="allowed",
                    last_indexed=last_indexed,
                    owner=owner_id,
                    document_id=passage.document_id,
                    score=passage.score,
                )
            )
        return results

    # --- optional cited direct answer (INV-3) -------------------------------

    async def _direct_answer(
        self, query: str, results: list[SearchResultData]
    ) -> DirectAnswerData | None:
        """Optionally ground a short cited answer on the top permitted results.

        Only attempted when a model provider is configured (the gateway is
        otherwise no-op'd); a model/dependency error is swallowed to a ``None``
        answer rather than failing the whole search — the results are the contract
        guarantee; the answer is optional (mission filter #2). The citations
        reference result ids present in ``results`` (INV-3): the answer can never
        cite a passage the caller could not retrieve. When the model produces no
        usable text, the answer is omitted (prefer "no answer" over an unsourced
        claim).
        """
        if not self._gateway.enabled:
            return None
        top = results[:_DIRECT_ANSWER_TOP_K]
        if not top:
            return None
        try:
            completion = await self._gateway.chat(
                self._grounding_messages(query, top),
                max_tokens=_DIRECT_ANSWER_MAX_TOKENS,
            )
        except AppError as exc:
            # Never fail the search on the optional answer; log the *type* only.
            log.info("search.direct_answer_skipped", code=exc.code)
            return None
        text = completion.content.strip()
        if not text:
            return None
        # Cite every grounded result (each is a permitted passage present in the
        # page) — INV-3 is structural: the citation set is a subset of `results`.
        citations = [
            SearchCitationData(
                result_id=r.id,
                snippet=r.snippet[:_GROUNDING_SNIPPET_CHARS],
                char_start=0,
                char_end=min(len(r.snippet), _GROUNDING_SNIPPET_CHARS),
            )
            for r in top
        ]
        return DirectAnswerData(text=text, citations=citations)

    @staticmethod
    def _grounding_messages(query: str, results: list[SearchResultData]) -> list[ChatMessage]:
        """Build the grounded prompt: answer ONLY from the numbered result passages."""
        sources = "\n\n".join(
            f"[{i + 1}] {r.title}: {r.snippet[:_GROUNDING_SNIPPET_CHARS]}"
            for i, r in enumerate(results)
        )
        system = (
            "You are Lumen Copilot. Answer the user's question using ONLY the "
            "numbered source passages below. If they do not answer it, reply "
            "exactly: I couldn't find an answer in your sources. Do not use outside "
            "knowledge and do not invent facts.\n\nSources:\n" + sources
        )
        return [
            ChatMessage(role=Role.SYSTEM, content=system),
            ChatMessage(role=Role.USER, content=query),
        ]

    # --- audit (INV-6) ------------------------------------------------------

    async def _audit_search(
        self,
        *,
        query: str,
        results: list[SearchResultData],
        collection_id: UUID | None,
        has_direct_answer: bool,
    ) -> None:
        """Emit exactly one ``retrieval.query`` audit event for this search (INV-6).

        Records the non-reversible query hash (never the raw text, spec 0004
        §2.4), the document ids surfaced, the hit count, and whether a direct
        answer was synthesized. ``resource_id`` is the query hash so a reviewer can
        correlate without storing the question.
        """
        query_hash = _hash_query(query)
        document_ids = sorted({str(r.document_id) for r in results if r.document_id is not None})
        await self._audit.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="search",
            resource_id=query_hash,
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "query_hash": query_hash,
                "hit_count": len(results),
                "document_ids": document_ids,
                "collection_id": str(collection_id) if collection_id is not None else None,
                "direct_answer": has_direct_answer,
            },
        )


def _why_matched(snippet: str, pattern: re.Pattern[str] | None) -> str:
    """A short human-readable reason this result ranked (the contract ``why_matched``).

    Lexical when the snippet literally contains a query term (we can point at the
    span); otherwise semantic (the hybrid retrieval fused a vector-similarity hit
    with no surface term overlap). The hybrid retrieval always contributes the
    ranking, so both forms are honest descriptions of why it surfaced.
    """
    if pattern is not None and pattern.search(snippet):
        return "Matched your search terms in the text (lexical + semantic)."
    return "Semantically related to your query."


__all__ = [
    "DirectAnswerData",
    "MatchSpanData",
    "SearchCitationData",
    "SearchPage",
    "SearchResultData",
    "SearchService",
]
