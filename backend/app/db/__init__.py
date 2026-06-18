"""Relational database adapter — the only SQL in the backend.

Single responsibility (ADR-0004 boundary table): own the async SQLAlchemy
engine, the session factory, the declarative ``Base``, and (as features land)
models + intent-named repositories returning domain types. **Nobody else may
import SQLAlchemy sessions/models or write SQL.** Routers and services receive
a session via the dependency in ``app.api.deps``; repositories never leak a
``Session`` upward.
"""

from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.db.session import (
    dispose_engine,
    get_engine,
    get_sessionmaker,
    session_scope,
)

__all__ = [
    "AuditEventRepository",
    "Base",
    "ChatSessionRepository",
    "ChunkRepository",
    "CitationRepository",
    "CollectionRepository",
    "DocumentRepository",
    "MessageRepository",
    "TenantRepository",
    "UserRepository",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
]
