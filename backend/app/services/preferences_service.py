"""User account-preferences use-cases (spec 0005, epic #144).

The orchestration behind ``GET``/``PATCH /preferences`` (ADR-0004: ``services/``
compose the ``db/`` repositories; the router calls exactly one service). Per-user
*and* tenant-scoped: the repository binds the tenant, this service the
``user_id`` — both from the resolved principal, never request input (spec 0004
§2.3, INV-1/INV-2). A fresh user has no row: :meth:`get` returns the implicit
server-default state **without** writing (read-before-write); the row is created
lazily on the first :meth:`set_default_model`.

The ``default_model`` override is validated against the #47 model registry on
write (unknown → 422, INV-8) and **fail-closed** at chat time:
:meth:`resolved_default_model` ignores a stored model that has since left the
registry and falls back to the server default, so a removed model never strands a
user's chats (spec 0005 AC-P4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.db.repositories import UserPreferenceRepository
from app.services.models_service import is_allowed_model


@dataclass(frozen=True, slots=True)
class PreferencesView:
    """The caller's preferences projected for the wire (contract ``UserPreferences``).

    ``default_model_id`` is the stored override, or ``None`` to use the server
    default; ``updated_at`` is ``None`` for a fresh user who never set one.
    """

    default_model_id: str | None
    updated_at: datetime | None


_EMPTY = PreferencesView(default_model_id=None, updated_at=None)


def _server_default(settings: Settings) -> str:
    """The registry's ``is_default`` model id (the settings validator guarantees one)."""
    for model in settings.chat_model_registry:
        if model.is_default:
            return model.id
    return settings.chat_model_registry[0].id


class PreferencesService:
    """Read / update the caller's account preferences (spec 0005).

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``user_id`` (both from the token), and the process ``Settings`` (the model
    allow-list + server default).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        settings: Settings,
    ) -> None:
        self._prefs = UserPreferenceRepository(session, tenant_id)
        self._user_id = user_id
        self._settings = settings

    async def get(self) -> PreferencesView:
        """The caller's preferences, or the implicit default state — **no write**.

        A user who has never set a preference has no row; rather than create one
        from a read, return the server-default state (``default_model_id`` /
        ``updated_at`` both ``None``) so a GET never mutates state (spec 0005 §2).
        """
        prefs = await self._prefs.get(self._user_id)
        if prefs is None:
            return _EMPTY
        return PreferencesView(default_model_id=prefs.default_model, updated_at=prefs.updated_at)

    async def set_default_model(self, default_model_id: str | None) -> PreferencesView:
        """Set (or clear, with ``None``) the caller's default-model override.

        A non-null id must be in the #47 model registry, else **422**
        ``unknown_model`` (INV-8) and nothing is persisted. The preferences row is
        created lazily on the first write.
        """
        if default_model_id is not None and not is_allowed_model(default_model_id, self._settings):
            raise ValidationError(f"Unknown model {default_model_id!r}.", code="unknown_model")
        prefs = await self._prefs.set_default_model(self._user_id, default_model_id)
        return PreferencesView(default_model_id=prefs.default_model, updated_at=prefs.updated_at)

    async def resolved_default_model(self) -> str:
        """The caller's effective default model for a **new** chat (fail-closed).

        Their stored override if set *and* still in the registry; otherwise the
        server default. A stored model that has since left the registry is ignored
        rather than erroring — a removed model never strands a chat (AC-P4).
        """
        prefs = await self._prefs.get(self._user_id)
        if (
            prefs is not None
            and prefs.default_model is not None
            and is_allowed_model(prefs.default_model, self._settings)
        ):
            return prefs.default_model
        return _server_default(self._settings)
