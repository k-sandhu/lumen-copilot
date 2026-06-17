"""FastAPI dependencies — the wiring routers pull from.

Provides the request-scoped DB session, the settings accessor, and the adapter
singletons (LLM gateway, object store) so routers and (later) services receive
them by injection rather than constructing them. Identity/tenant dependencies
(``current_user``/``current_tenant``) land here under ``app.auth`` (CC-3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.llm import LLMGateway
from app.storage import ObjectStore


def get_settings_dep() -> Settings:
    """Settings dependency (delegates to the cached singleton)."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped async DB session.

    The session is closed when the request ends; it is committed by the caller
    on success. This is the only way a router/service obtains a session — the
    session factory itself stays inside ``app.db``.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@lru_cache(maxsize=1)
def get_llm_gateway() -> LLMGateway:
    """Process-wide LLM gateway singleton."""
    return LLMGateway(get_settings())


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Process-wide object-store adapter singleton."""
    return ObjectStore(get_settings())
