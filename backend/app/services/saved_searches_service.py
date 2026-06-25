"""Saved-searches use-cases (spec 0005, epic #144).

Owner- and tenant-scoped CRUD over the saved-searches repository (ADR-0004:
``services/`` compose ``db/``; the router calls exactly one service). A saved
search in another tenant — or owned by another user — is treated as
**non-existent**: the read / update / delete returns ``None`` / ``False`` and the
router maps that to **404** (existence non-disclosure; never 403). The
``owner_id`` / ``tenant_id`` come from the resolved principal, never request input
(spec 0004 §2.1/§2.2, INV-1/INV-2).

Cursor pagination mirrors the chat keyset: an opaque cursor encodes the boundary
row **id**; the repository resolves its timestamp in-DB. A malformed cursor is
rejected fail-closed (422).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.db.repositories import SavedSearchRepository
from app.domain.entities import SavedSearch

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20
_CURSOR_PREFIX = "svs:"


def _encode_cursor(row_id: UUID) -> str:
    raw = f"{_CURSOR_PREFIX}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


@dataclass(frozen=True, slots=True)
class SavedSearchPage:
    """One page of saved searches + the opaque cursor for the next page."""

    items: list[SavedSearch]
    next_cursor: str | None


class SavedSearchService:
    """Create / list / get / update / delete the caller's saved searches.

    Constructed per-request with the session and the resolved ``tenant_id`` /
    ``owner_id`` (both from the token). All ownership/tenancy enforcement lives
    here.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: UUID, owner_id: UUID) -> None:
        self._repo = SavedSearchRepository(session, tenant_id)
        self._owner_id = owner_id

    def _owns(self, saved: SavedSearch) -> bool:
        return saved.owner_id == self._owner_id

    async def create(
        self,
        *,
        name: str,
        query: str,
        collection_id: UUID | None,
        source: str | None,
        type: str | None,
    ) -> SavedSearch:
        """Save a search owned by the caller (the wire fields are pre-validated)."""
        return await self._repo.create(
            owner_id=self._owner_id,
            name=name,
            query=query,
            collection_id=collection_id,
            source=source,
            type=type,
        )

    async def list(self, *, cursor: str | None, limit: int | None) -> SavedSearchPage:
        """A keyset page of the caller's own saved searches (newest-updated first)."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._repo.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return SavedSearchPage(items=page, next_cursor=next_cursor)

    async def get(self, saved_search_id: UUID) -> SavedSearch | None:
        """Fetch one of the caller's saved searches, or ``None`` if not visible (→ 404)."""
        saved = await self._repo.get(saved_search_id)
        if saved is None or not self._owns(saved):
            return None
        return saved

    async def update(
        self,
        saved_search_id: UUID,
        *,
        name: str | None,
        query: str | None,
        collection_id: UUID | None,
        set_collection_id: bool,
        source: str | None,
        set_source: bool,
        type: str | None,
        set_type: bool,
    ) -> SavedSearch | None:
        """Edit one of the caller's saved searches; ``None`` if not visible (→ 404).

        Visibility (tenant + ownership) is established before any write so a
        non-owner's saved search is never mutated and is reported as 404 (INV-2).
        Nullable filters are tri-state (``set_*`` flags pass through to the repo).
        """
        existing = await self._repo.get(saved_search_id)
        if existing is None or not self._owns(existing):
            return None
        return await self._repo.update(
            saved_search_id,
            name=name,
            query=query,
            collection_id=collection_id,
            set_collection_id=set_collection_id,
            source=source,
            set_source=set_source,
            type=type,
            set_type=set_type,
        )

    async def delete(self, saved_search_id: UUID) -> bool:
        """Delete one of the caller's saved searches; ``False`` if not visible (→ 404)."""
        existing = await self._repo.get(saved_search_id)
        if existing is None or not self._owns(existing):
            return False
        return await self._repo.delete(saved_search_id)
