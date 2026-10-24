"""UUID-only handle resolution for retrieval fakes that do not model a catalog."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.retrieval import DocumentResolution, DocumentText


class UUIDDocumentResolver(ABC):
    """Adapt a fake's own UUID read without duplicating parsing across tests."""

    @abstractmethod
    async def get_document(self, *, principal: object, document_id: UUID) -> DocumentText | None:
        raise NotImplementedError

    async def resolve_document_text(self, *, principal: object, handle: str) -> DocumentResolution:
        try:
            document_id = UUID(handle.strip())
        except ValueError:
            return DocumentResolution()
        return DocumentResolution(
            document=await self.get_document(principal=principal, document_id=document_id)
        )
