"""Account-preferences routes — ``GET``/``PATCH /preferences`` (spec 0005, epic #144).

Contract-first: shapes match ``contracts/openapi.yaml`` (``UserPreferences`` /
``UserPreferencesUpdate``). The router validates in → calls **one** service →
shapes out (ADR-0004). Always the **caller's** row: there is no id in the path and
no way to read or write another user's preferences (INV-2). ``GET`` never writes
(a fresh user gets the implicit default state); an unknown ``default_model_id`` on
``PATCH`` is **422** (INV-8). Every route requires the bearer token (INV-4).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, model_validator

from app.api.deps import CurrentTenant, CurrentUser, DbSession, SettingsDep
from app.services.preferences_service import PreferencesService

router = APIRouter(prefix="/preferences", tags=["preferences"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class UserPreferencesResponse(BaseModel):
    """``#/components/schemas/UserPreferences``."""

    model_config = {"extra": "forbid"}

    default_model_id: str | None = None
    updated_at: datetime | None = None


class UserPreferencesUpdate(BaseModel):
    """``#/components/schemas/UserPreferencesUpdate`` — partial; at least one field."""

    model_config = {"extra": "forbid"}

    default_model_id: str | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> UserPreferencesUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


@router.get("", response_model=UserPreferencesResponse)
async def get_preferences(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> UserPreferencesResponse:
    """The caller's preferences (a fresh user gets the implicit default; no write)."""
    service = PreferencesService(
        session, tenant_id=tenant_id, user_id=principal.user_id, settings=settings
    )
    view = await service.get()
    return UserPreferencesResponse(
        default_model_id=view.default_model_id, updated_at=view.updated_at
    )


@router.patch("", response_model=UserPreferencesResponse)
async def update_preferences(
    body: UserPreferencesUpdate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> UserPreferencesResponse:
    """Set or clear the caller's default-model override (unknown id → 422)."""
    service = PreferencesService(
        session, tenant_id=tenant_id, user_id=principal.user_id, settings=settings
    )
    view = await service.set_default_model(body.default_model_id)
    await session.commit()
    return UserPreferencesResponse(
        default_model_id=view.default_model_id, updated_at=view.updated_at
    )
