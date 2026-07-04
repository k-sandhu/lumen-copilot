"""Chat-model registry service (issue #47).

The single use-case behind ``GET /models`` and the reusable allow-list check the
chat runtime (#24) will call before accepting a caller-supplied ``model``. Per
ADR-0004 layering the router calls **one** service and shapes its result; the
registry itself is config (``core/config.py``), so this layer only reads the
settings, maps each :class:`~app.core.config.ChatModelSetting` to the domain
:class:`~app.domain.models.ChatModel`, and answers membership queries.

The ``is_allowed_model`` helper is exported both as a free function (the form
the issue names for the chat runtime) and as a method, so a future caller that
already holds a :class:`ChatModelService` need not re-read settings.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ChatModelSetting, Settings
from app.db.repositories import LlmProviderRepository
from app.domain.models import ChatModel
from app.services.provider_models import (
    is_allowed_provider_model,
    is_provider_model_id,
    list_provider_models,
)


def _to_domain(setting: ChatModelSetting) -> ChatModel:
    """Map a config registry row to its domain value object."""
    return ChatModel(
        id=setting.id,
        label=setting.label,
        provider=setting.provider,
        tier=setting.tier,
        is_default=setting.is_default,
        description=setting.description,
    )


class ChatModelService:
    """Reads the curated model registry + surfaces the tenant's provider models.

    The config registry is settings (validated at startup: exactly one default,
    unique ids), so the config half holds no I/O. The provider half reads the
    caller's tenant's ENABLED :class:`~app.domain.entities.LlmProvider` rows via a
    tenant-scoped repository (INV-1) and maps their ``discovered_models`` to
    namespaced picker entries (PR 2a). Constructed per request from the injected
    ``Settings`` plus — when the tenant's provider models are wanted — a session +
    ``tenant_id``. Omitting the session yields config-only behaviour (unchanged).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session: AsyncSession | None = None,
        tenant_id: UUID | None = None,
    ) -> None:
        self._settings = settings
        # The tenant's provider repository, only when a session + tenant were
        # supplied (the surfacing/routing path); config-only callers pass neither.
        self._providers = (
            LlmProviderRepository(session, tenant_id)
            if session is not None and tenant_id is not None
            else None
        )

    def list_models(self) -> list[ChatModel]:
        """The curated registry as domain models, in configured order.

        Order is preserved from config so the picker renders tiers in the
        intended sequence (frontier → fast → oss in the seed set). Exactly one
        carries ``is_default`` (enforced by the settings validator, AC-1). This is
        the config half only — see :meth:`list_all_models` for the tenant's
        provider models too.
        """
        return [_to_domain(m) for m in self._settings.chat_model_registry]

    async def list_all_models(self) -> list[ChatModel]:
        """The config registry PLUS the tenant's ENABLED provider models (PR 2a).

        Config models first (unchanged ids + order), then every enabled provider's
        discovered models mapped to namespaced picker entries. When no tenant
        session was supplied this is just the config registry (config-only fallback,
        so an offline caller degrades gracefully).
        """
        models = self.list_models()
        if self._providers is not None:
            models.extend(await list_provider_models(self._providers))
        return models

    def is_allowed_model(self, model_id: str) -> bool:
        """True iff ``model_id`` is in the curated registry (issue #47 AC-3).

        The chat runtime calls this to reject an unknown ``model`` with a 422
        before any provider call; the registry is the allow-list. Matching is
        exact on the provider-qualified id (e.g. ``anthropic/claude-opus-4.8``).
        This is the config half only — see :meth:`is_allowed_model_async` for
        provider ids.
        """
        return is_allowed_model(model_id, self._settings)

    async def is_allowed_model_async(self, model_id: str) -> bool:
        """True iff ``model_id`` is a config OR a valid tenant provider model (PR 2a).

        A ``provider:`` id is checked against the tenant's enabled providers +
        their discovered snapshot (unknown / disabled / cross-tenant → False); any
        other id falls back to the config allow-list. The DB-aware allow-list the
        chat send path uses so a discovered provider model is selectable.
        """
        if is_provider_model_id(model_id):
            if self._providers is None:
                return False
            return await is_allowed_provider_model(model_id, self._providers)
        return self.is_allowed_model(model_id)


def is_allowed_model(model_id: str, settings: Settings) -> bool:
    """Free-function allow-list check the chat runtime (#24) reuses.

    Returns ``True`` iff ``model_id`` is a registered model id. An unknown id is
    the basis for the future chat-runtime 422 (INV-8) — this helper draws no
    conclusion about *how* a caller reacts, only whether the id is allowed.
    """
    return any(m.id == model_id for m in settings.chat_model_registry)
