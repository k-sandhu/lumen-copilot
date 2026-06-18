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

from app.core.config import ChatModelSetting, Settings
from app.domain.models import ChatModel


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
    """Reads the curated model registry from config and answers about it.

    Holds no I/O — the registry is settings, validated at startup (exactly one
    default, unique ids). Constructed per request from the injected ``Settings``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def list_models(self) -> list[ChatModel]:
        """The curated registry as domain models, in configured order.

        Order is preserved from config so the picker renders tiers in the
        intended sequence (frontier → fast → oss in the seed set). Exactly one
        carries ``is_default`` (enforced by the settings validator, AC-1).
        """
        return [_to_domain(m) for m in self._settings.chat_model_registry]

    def is_allowed_model(self, model_id: str) -> bool:
        """True iff ``model_id`` is in the curated registry (issue #47 AC-3).

        The chat runtime calls this to reject an unknown ``model`` with a 422
        before any provider call; the registry is the allow-list. Matching is
        exact on the provider-qualified id (e.g. ``anthropic/claude-opus-4.8``).
        """
        return is_allowed_model(model_id, self._settings)


def is_allowed_model(model_id: str, settings: Settings) -> bool:
    """Free-function allow-list check the chat runtime (#24) reuses.

    Returns ``True`` iff ``model_id`` is a registered model id. An unknown id is
    the basis for the future chat-runtime 422 (INV-8) — this helper draws no
    conclusion about *how* a caller reacts, only whether the id is allowed.
    """
    return any(m.id == model_id for m in settings.chat_model_registry)
