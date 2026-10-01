"""Version 2 direct-upload/media routes (spec 0008)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v2.documents import router as documents_router
from app.api.v2.uploads import router as uploads_router

router = APIRouter()
router.include_router(uploads_router)
router.include_router(documents_router)

__all__ = ["router"]
