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
from app.db.repositories import _UNSET, UserPreferenceRepository, _Unset
from app.domain.entities import UserPreferences
from app.services.models_service import is_allowed_model


@dataclass(frozen=True, slots=True)
class PreferencesView:
    """The caller's preferences projected for the wire (contract ``UserPreferences``).

    ``default_model_id`` is the stored override, or ``None`` to use the server
    default; ``custom_instructions`` is the stored per-user preamble prepended to
    the chat system prompt, or ``None``; ``updated_at`` is ``None`` for a fresh user
    who never set one.
    """

    default_model_id: str | None
    custom_instructions: str | None
    updated_at: datetime | None


_EMPTY = PreferencesView(default_model_id=None, custom_instructions=None, updated_at=None)

# Per-user custom-instructions cap. A generous free-text preamble (persona / tone /
# standing context) but bounded so it can't crowd out the grounding contract or blow
# the prompt budget — enforced at the wire (422 over-limit) and documented here as
# the single source of truth.
MAX_CUSTOM_INSTRUCTIONS_CHARS = 2000


def _server_default(settings: Settings) -> str:
    """The registry's ``is_default`` model id (the settings validator guarantees one)."""
    for model in settings.chat_model_registry:
        if model.is_default:
            return model.id
    return settings.chat_model_registry[0].id


def _view(prefs: UserPreferences) -> PreferencesView:
    """Project a stored preferences entity into the wire view."""
    return PreferencesView(
        default_model_id=prefs.default_model,
        custom_instructions=prefs.custom_instructions,
        updated_at=prefs.updated_at,
    )


def _normalize_instructions(value: str | None) -> str | None:
    """Trim custom instructions, treat blank as clear, and enforce the cap (422 over).

    A blank/whitespace-only string is normalized to ``None`` (clear) so the stored
    state and the "no instructions" case coincide; a value over the cap is rejected
    as a **422** ``instructions_too_long`` before anything is persisted (INV-8).
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_CUSTOM_INSTRUCTIONS_CHARS:
        raise ValidationError(
            f"Custom instructions must be at most {MAX_CUSTOM_INSTRUCTIONS_CHARS} characters.",
            code="instructions_too_long",
        )
    return trimmed


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
        return _view(prefs)

    async def set_default_model(self, default_model_id: str | None) -> PreferencesView:
        """Set (or clear, with ``None``) the caller's default-model override.

        A non-null id must be in the #47 model registry, else **422**
        ``unknown_model`` (INV-8) and nothing is persisted. The preferences row is
        created lazily on the first write.
        """
        if default_model_id is not None and not is_allowed_model(default_model_id, self._settings):
            raise ValidationError(f"Unknown model {default_model_id!r}.", code="unknown_model")
        prefs = await self._prefs.set_default_model(self._user_id, default_model_id)
        return _view(prefs)

    async def update(
        self,
        *,
        default_model_id: str | None | _Unset = _UNSET,
        custom_instructions: str | None | _Unset = _UNSET,
    ) -> PreferencesView:
        """Apply a partial preferences update (tri-state per field).

        Each field is omitted (``_UNSET`` ⇒ unchanged), set to a value, or set to
        ``None`` (clear) — mirroring how ``PATCH /preferences`` distinguishes an
        absent field from an explicit null. A non-null ``default_model_id`` must be in
        the #47 registry, else **422** ``unknown_model`` (INV-8). ``custom_instructions``
        is trimmed; a blank string clears it, and it is validated against the cap
        (``MAX_CUSTOM_INSTRUCTIONS_CHARS``) — over-limit is **422** ``instructions_too_long``.
        Nothing is persisted if validation fails. The row is created lazily on first write.
        """
        if (
            not isinstance(default_model_id, _Unset)
            and default_model_id is not None
            and not is_allowed_model(default_model_id, self._settings)
        ):
            raise ValidationError(f"Unknown model {default_model_id!r}.", code="unknown_model")

        normalized_instructions: str | None | _Unset
        if isinstance(custom_instructions, _Unset):
            normalized_instructions = _UNSET
        else:
            normalized_instructions = _normalize_instructions(custom_instructions)

        prefs = await self._prefs.update(
            self._user_id,
            default_model=default_model_id,
            custom_instructions=normalized_instructions,
        )
        return _view(prefs)

    async def resolved_custom_instructions(self) -> str | None:
        """The caller's stored custom instructions for a chat turn, or ``None``.

        Read-only lookup used by the chat send path to thread the user's preamble into
        the composed system prompt (before the grounding contract). A fresh user (no
        row) or a cleared value yields ``None`` — ad-hoc chat, unchanged.
        """
        prefs = await self._prefs.get(self._user_id)
        if prefs is None:
            return None
        return prefs.custom_instructions

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
