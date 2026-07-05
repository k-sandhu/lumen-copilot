"""Account-preferences routes — ``GET``/``PATCH /preferences`` (spec 0005, epic #144).

Contract-first: shapes match ``contracts/openapi.yaml`` (``UserPreferences`` /
``UserPreferencesUpdate``). The router validates in → calls **one** service →
shapes out (ADR-0004). Always the **caller's** row: there is no id in the path and
no way to read or write another user's preferences (INV-2). ``GET`` never writes
(a fresh user gets the implicit default state); an unknown ``default_model_id`` or
over-cap ``custom_instructions`` on ``PATCH`` is **422** (INV-8). Every route
requires the bearer token (INV-4).

Two per-user settings live here: ``default_model_id`` (the chat default model,
epic #144) and ``custom_instructions`` (a free-text preamble prepended to the chat
system prompt — the injection is threaded through the chat send/runtime path, not
read here). Both are tri-state on ``PATCH`` (absent = unchanged, value = set, null
= clear).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentTenant, CurrentUser, DbSession, SettingsDep
from app.db.repositories import _UNSET
from app.services.preferences_service import (
    MAX_CUSTOM_INSTRUCTIONS_CHARS,
    PreferencesService,
    PreferencesView,
)

router = APIRouter(prefix="/preferences", tags=["preferences"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class UserPreferencesResponse(BaseModel):
    """``#/components/schemas/UserPreferences``."""

    model_config = {"extra": "forbid"}

    default_model_id: str | None = None
    custom_instructions: str | None = None
    updated_at: datetime | None = None


class UserPreferencesUpdate(BaseModel):
    """``#/components/schemas/UserPreferencesUpdate`` — partial; at least one field.

    Both fields are tri-state on the wire: absent = leave unchanged, a value = set,
    ``null`` = clear (fall back to the server default / no instructions). Pydantic
    collapses "absent" and "explicit null" to ``None``, so the handler consults
    ``__pydantic_fields_set__`` to tell them apart. ``custom_instructions`` is capped
    at ``MAX_CUSTOM_INSTRUCTIONS_CHARS`` at the wire (over-limit → 422, INV-8).
    """

    model_config = {"extra": "forbid"}

    default_model_id: str | None = None
    custom_instructions: str | None = Field(
        default=None, max_length=MAX_CUSTOM_INSTRUCTIONS_CHARS
    )

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
    return _to_response(view)


@router.patch("", response_model=UserPreferencesResponse)
async def update_preferences(
    body: UserPreferencesUpdate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> UserPreferencesResponse:
    """Set or clear the caller's default model and/or custom instructions.

    Tri-state per field (absent = unchanged, value = set, null = clear): an unknown
    ``default_model_id`` → 422 ``unknown_model``; over-cap ``custom_instructions`` →
    422 (the wire ``max_length``). Only the fields the client actually sent are
    applied (``__pydantic_fields_set__``).
    """
    service = PreferencesService(
        session, tenant_id=tenant_id, user_id=principal.user_id, settings=settings
    )
    sent = body.__pydantic_fields_set__
    view = await service.update(
        default_model_id=(
            body.default_model_id if "default_model_id" in sent else _UNSET
        ),
        custom_instructions=(
            body.custom_instructions if "custom_instructions" in sent else _UNSET
        ),
    )
    await session.commit()
    return _to_response(view)


def _to_response(view: PreferencesView) -> UserPreferencesResponse:
    return UserPreferencesResponse(
        default_model_id=view.default_model_id,
        custom_instructions=view.custom_instructions,
        updated_at=view.updated_at,
    )
