"""Owner/tenant-scoped storage for durable evidence identities."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.errors import NotFoundError, ValidationError
from app.db import models
from app.domain.chat import WebCitation


class HandleRepository:
    def __init__(self, session: AsyncSession, principal: Principal, session_id: UUID) -> None:
        self._session = session
        self._principal = principal
        self._session_id = session_id

    async def reserve(self, capacity: int = 1000) -> int:
        if not 1 <= capacity <= 1000:
            raise ValidationError("Invalid evidence handle reservation.")
        stmt = (
            update(models.ChatSession)
            .where(
                models.ChatSession.id == self._session_id,
                models.ChatSession.tenant_id == self._principal.tenant_id,
                models.ChatSession.owner_id == self._principal.user_id,
            )
            .values(handle_next=models.ChatSession.handle_next + capacity)
            .returning(models.ChatSession.handle_next)
        )
        next_value = (await self._session.execute(stmt)).scalar_one_or_none()
        if next_value is None:
            raise NotFoundError("Conversation not found.")
        return int(next_value) - capacity

    async def _check_owner(self) -> None:
        stmt = select(models.ChatSession.id).where(
            models.ChatSession.id == self._session_id,
            models.ChatSession.tenant_id == self._principal.tenant_id,
            models.ChatSession.owner_id == self._principal.user_id,
        )
        if (await self._session.execute(stmt)).scalar_one_or_none() is None:
            raise NotFoundError("Conversation not found.")

    async def load(self) -> dict[str, dict[str, object]]:
        await self._check_owner()
        rows = (
            await self._session.execute(
                select(models.SourceHandle).where(
                    models.SourceHandle.tenant_id == self._principal.tenant_id,
                    models.SourceHandle.session_id == self._session_id,
                )
            )
        ).scalars()
        return {row.handle: dict(row.evidence) for row in rows}

    async def append(self, entries: dict[str, dict[str, object]]) -> None:
        existing = await self.load()
        for handle, evidence in entries.items():
            if handle in existing:
                if existing[handle] != evidence:
                    raise ValidationError("Evidence handle cannot be reassigned.")
                continue
            self._session.add(
                models.SourceHandle(
                    tenant_id=self._principal.tenant_id,
                    session_id=self._session_id,
                    handle=handle,
                    evidence=dict(evidence),
                )
            )
        await self._session.flush()


class WebCitationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(self, message_id: UUID, citation: WebCitation) -> None:
        self._session.add(
            models.WebCitation(
                id=citation.id,
                tenant_id=self._tenant_id,
                message_id=message_id,
                handle=citation.handle,
                url=citation.url,
                title=citation.title,
                snippet=citation.snippet,
            )
        )
        await self._session.flush()

    async def list_for_messages(self, message_ids: list[UUID]) -> dict[UUID, list[WebCitation]]:
        rows = (
            await self._session.execute(
                select(models.WebCitation).where(
                    models.WebCitation.tenant_id == self._tenant_id,
                    models.WebCitation.message_id.in_(message_ids),
                )
            )
        ).scalars()
        result: dict[UUID, list[WebCitation]] = {}
        for row in rows:
            result.setdefault(row.message_id, []).append(
                WebCitation(
                    id=row.id, handle=row.handle, url=row.url, title=row.title, snippet=row.snippet
                )
            )
        return result
