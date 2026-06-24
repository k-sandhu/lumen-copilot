"""Search typeahead suggestions (spec 0005, epic #144).

Builds the ``GET /search/suggest`` response: as the caller types, suggest from
their own **recent** + **saved** searches (``completion`` / ``saved_search``) and
from permitted **document** titles (``document``). Document matches go through the
**same** retrieval permission chokepoint as ``/search``
(:meth:`RetrievalService.search_documents`) — a title the caller cannot open is
never suggested (INV-2). All sources are per-user and tenant-scoped.

**Not audited per-keystroke.** This is a debounced typeahead over already-permitted
data; the actual ``/search`` on submit records the retrieval audit event *and* the
recent query. Auditing every keystroke would flood the audit log without adding a
provable decision beyond what ``/search`` records (spec 0005 §5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db.repositories import (
    RecentSearchRepository,
    SavedSearchRepository,
    normalize_query,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, RecentSearch
from app.retrieval import RetrievalService
from app.services.audit import AuditSink

# How many candidates to pull from each source before the merge (kept small — a
# typeahead only ever shows a handful, and the merge caps to the request limit).
_PER_SOURCE = 20


@dataclass(frozen=True, slots=True)
class SuggestionData:
    """One typeahead suggestion (the contract ``Suggestion``).

    ``kind`` ∈ ``completion`` | ``document`` | ``saved_search``; ``text`` is the
    label / the query to run; the id fields light up per kind.
    """

    kind: str
    text: str
    document_id: UUID | None = None
    source: str | None = None
    saved_search_id: UUID | None = None


class SuggestService:
    """Merge permission-trimmed document matches with the caller's recent/saved.

    Constructed per-request with the session, the resolved principal, the
    ``retrieval/`` chokepoint (for the permission-trimmed document matches), and —
    for the suggest surface — the audit sink + correlation ids so each suggest
    emits one ``retrieval.query`` audit event (INV-6; the contract treats suggest
    as a retrieval surface). The recent list/clear paths pass no audit (plain
    reads/writes of the caller's own history, not retrieval decisions).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        retrieval: RetrievalService,
        audit: AuditSink | None = None,
        request_id: str = "unknown",
        source_ip: str = "unknown",
    ) -> None:
        self._principal = principal
        self._retrieval = retrieval
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._recent = RecentSearchRepository(session, principal.tenant_id)
        self._saved = SavedSearchRepository(session, principal.tenant_id)

    async def suggest(self, *, q: str, limit: int) -> list[SuggestionData]:
        """Up to ``limit`` ranked suggestions for the partial query ``q``.

        Order: the caller's recent completions first (high intent), then permitted
        document titles, then matching saved searches — deduped by display text and
        capped at ``limit``. Document matches run through the SAME retrieval
        chokepoint as ``/search`` (a title the caller can't open is never returned,
        INV-2). Emits one ``retrieval.query`` audit event so the permission-trimmed
        lookup is provable after the fact (INV-6). A blank ``q`` yields ``[]`` and
        no event (the contract rejects an all-whitespace ``q`` with 422 first).
        """
        needle = normalize_query(q)
        if not needle:
            return []

        out: list[SuggestionData] = []
        seen: set[str] = set()

        def _add(suggestion: SuggestionData) -> None:
            key = suggestion.text.strip().lower()
            if not key or key in seen or len(out) >= limit:
                return
            seen.add(key)
            out.append(suggestion)

        # 1) Completions from the caller's own recent queries (most intentful).
        for recent in await self._recent.list_for_user(self._principal.user_id, limit=_PER_SOURCE):
            if needle in normalize_query(recent.query):
                _add(SuggestionData(kind="completion", text=recent.query))

        # 2) Permitted document titles — permission-trimmed via the retrieval
        #    chokepoint (a title the caller cannot open is never returned, INV-2).
        for doc in await self._retrieval.search_documents(
            principal=self._principal, name_or_query=q, k=_PER_SOURCE
        ):
            _add(
                SuggestionData(kind="document", text=doc.document_name, document_id=doc.document_id)
            )

        # 3) The caller's saved searches whose name matches.
        for sv in await self._saved.list_for_owner_page(self._principal.user_id, limit=_PER_SOURCE):
            if needle in normalize_query(sv.name):
                _add(SuggestionData(kind="saved_search", text=sv.name, saved_search_id=sv.id))

        result = out[:limit]
        await self._emit_audit(needle, result)
        return result

    async def _emit_audit(self, needle: str, suggestions: list[SuggestionData]) -> None:
        """Emit one ``retrieval.query`` event for the suggest (INV-6), if wired.

        Records a non-reversible query hash (never the raw text, spec 0004 §2.4),
        the suggestion count, and the permitted document ids surfaced — so a
        permission-trimmed title lookup is provable after the fact.
        """
        if self._audit is None:
            return
        query_hash = hashlib.sha256(needle.encode("utf-8")).hexdigest()
        document_ids = sorted(
            {str(s.document_id) for s in suggestions if s.document_id is not None}
        )
        await self._audit.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(self._principal.user_id),
            resource_type="suggest",
            resource_id=query_hash,
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "query_hash": query_hash,
                "suggestion_count": len(suggestions),
                "document_ids": document_ids,
            },
        )

    async def list_recent(self, *, limit: int) -> list[RecentSearch]:
        """The caller's recent queries, newest-used first (``GET /search/recent``)."""
        return await self._recent.list_for_user(self._principal.user_id, limit=limit)

    async def clear_recent(self) -> None:
        """Clear the caller's recent history (``DELETE /search/recent``; idempotent)."""
        await self._recent.clear_for_user(self._principal.user_id)
