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

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.db.repositories import (
    RecentSearchRepository,
    SavedSearchRepository,
    normalize_query,
)
from app.domain.entities import RecentSearch
from app.retrieval import RetrievalService

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

    Constructed per-request with the session, the resolved principal, and the
    ``retrieval/`` chokepoint (for the permission-trimmed document matches).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        retrieval: RetrievalService,
    ) -> None:
        self._principal = principal
        self._retrieval = retrieval
        self._recent = RecentSearchRepository(session, principal.tenant_id)
        self._saved = SavedSearchRepository(session, principal.tenant_id)

    async def suggest(self, *, q: str, limit: int) -> list[SuggestionData]:
        """Up to ``limit`` ranked suggestions for the partial query ``q``.

        Order: the caller's recent completions first (high intent), then permitted
        document titles, then matching saved searches — deduped by display text and
        capped at ``limit``. A blank ``q`` yields ``[]`` (the contract rejects an
        all-whitespace ``q`` with 422 before reaching here).
        """
        needle = normalize_query(q)
        if not needle:
            return []

        out: list[SuggestionData] = []
        seen: set[str] = set()

        def _add(suggestion: SuggestionData) -> bool:
            key = suggestion.text.strip().lower()
            if not key or key in seen:
                return False
            seen.add(key)
            out.append(suggestion)
            return len(out) >= limit

        # 1) Completions from the caller's own recent queries (most intentful).
        recents = await self._recent.list_for_user(self._principal.user_id, limit=_PER_SOURCE)
        for recent in recents:
            if needle in normalize_query(recent.query) and _add(
                SuggestionData(kind="completion", text=recent.query)
            ):
                return out

        # 2) Permitted document titles — permission-trimmed via the retrieval
        #    chokepoint (a title the caller cannot open is never returned, INV-2).
        docs = await self._retrieval.search_documents(
            principal=self._principal, name_or_query=q, k=_PER_SOURCE
        )
        for doc in docs:
            if _add(
                SuggestionData(kind="document", text=doc.document_name, document_id=doc.document_id)
            ):
                return out

        # 3) The caller's saved searches whose name matches.
        saved = await self._saved.list_for_owner_page(self._principal.user_id, limit=_PER_SOURCE)
        for sv in saved:
            if needle in normalize_query(sv.name) and _add(
                SuggestionData(kind="saved_search", text=sv.name, saved_search_id=sv.id)
            ):
                return out

        return out[:limit]

    async def list_recent(self, *, limit: int) -> list[RecentSearch]:
        """The caller's recent queries, newest-used first (``GET /search/recent``)."""
        return await self._recent.list_for_user(self._principal.user_id, limit=limit)

    async def clear_recent(self) -> None:
        """Clear the caller's recent history (``DELETE /search/recent``; idempotent)."""
        await self._recent.clear_for_user(self._principal.user_id)
