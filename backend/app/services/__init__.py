"""Services layer — use-cases / orchestration.

Single responsibility (ADR-0004 layering): the only layer that *composes*
adapters (``db``, ``llm``, ``retrieval``, ``storage``, ``tasks``, ``realtime``,
``auth``) to fulfil a use-case. Holds *what the app does*. Routers call exactly
one service and shape its result; services hold no HTTP concerns and return
domain types. Dependencies point inward (``api -> services -> domain``); a
service never imports a router.

The first cross-cutting service is the product-audit sink (:mod:`app.services.audit`,
CC-8) — the single ``emit(...)`` seam every later feature calls to satisfy the
"auditable" mission filter (spec 0004 §2.4). Feature use-cases land here next.
"""

from app.services.audit import AuditSink
from app.services.collections_service import CollectionsService
from app.services.document_service import DocumentService
from app.services.models_service import ChatModelService, is_allowed_model

__all__ = [
    "AuditSink",
    "ChatModelService",
    "CollectionsService",
    "DocumentService",
    "is_allowed_model",
]
