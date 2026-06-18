"""Versioned API routes (``/api/v1``).

Feature routers (chat, ingestion, connectors, citations) mount here as their
cross-cuttings land, contract-first per ``contracts/openapi.yaml`` (ADR-0006).
``app.main`` includes this package's ``router`` under the ``/api/v1`` prefix.
Empty by design in the skeleton — only the seam exists.

Example (later):
    from app.api.v1.chat import router as chat_router
    router.include_router(chat_router)
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.collections import router as collections_router
from app.api.v1.models import router as models_router

# Aggregating router for all v1 feature routes. Mounted at /api/v1 in app.main.
router = APIRouter()
router.include_router(auth_router)
router.include_router(collections_router)
router.include_router(models_router)
