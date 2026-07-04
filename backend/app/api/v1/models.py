"""Model-picker routes — GET /models (issue #47).

Contract-first: shapes match ``contracts/openapi.yaml`` (``ChatModelInfo`` /
``ModelList``). The router validates in → calls **one** service
(:class:`~app.services.models_service.ChatModelService`) → shapes out
(ADR-0004); the curated registry is config (``core/config.py``), so there is no
business logic or model list hardcoded here.

The endpoint is bearer-protected: it depends on ``current_user`` (#19), so a
missing/invalid token is a 401 (INV-4) before any registry is read. The curated
config registry is the same for every tenant, but a tenant's ENABLED per-tenant
LLM providers surface their discovered models here too (PR 2a) — so the route is
now tenant-scoped (``current_tenant`` + a DB session) to read those providers.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentTenant, CurrentUser, DbSession, SettingsDep
from app.domain.models import ChatModel, ModelTier
from app.services.models_service import ChatModelService

router = APIRouter(tags=["models"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class ChatModelInfo(BaseModel):
    """``#/components/schemas/ChatModelInfo`` — one picker entry."""

    model_config = {"extra": "forbid"}

    id: str
    label: str
    provider: str
    tier: ModelTier
    is_default: bool
    description: str | None = None


class ModelList(BaseModel):
    """``#/components/schemas/ModelList`` — the curated registry."""

    model_config = {"extra": "forbid"}

    items: list[ChatModelInfo]


def _to_wire(model: ChatModel) -> ChatModelInfo:
    """Project a domain model to its contract wire shape."""
    return ChatModelInfo(
        id=model.id,
        label=model.label,
        provider=model.provider,
        tier=model.tier,
        is_default=model.is_default,
        description=model.description,
    )


# --- Routes -----------------------------------------------------------------


@router.get("/models", response_model=ModelList, response_model_exclude_none=True)
async def list_models(
    _principal: CurrentUser,
    settings: SettingsDep,
    session: DbSession,
    tenant_id: CurrentTenant,
) -> ModelList:
    """The curated registry PLUS the tenant's enabled provider models (AC-1 / PR 2a).

    Config models come first (grouped by tier in configured order with exactly one
    ``is_default`` — guaranteed by the settings validator, not the router); then
    each ENABLED per-tenant LLM provider's discovered models, mapped to namespaced
    ids the chat picker can select and the send path routes on. Requires a valid
    bearer token (``current_user``); unauthenticated → 401 (INV-4 / AC-4).
    """
    service = ChatModelService(settings, session=session, tenant_id=tenant_id)
    return ModelList(items=[_to_wire(m) for m in await service.list_all_models()])
