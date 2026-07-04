"""Per-tenant provider-model id namespacing + resolution (PR 2a: chat routing).

The single source of truth for how a discovered per-tenant LLM provider model is
projected into the chat picker and routed back through its provider. It bridges
the #47 config registry (built-in models, unchanged ids) and the foundation PR's
``llm_providers`` catalog (:class:`~app.domain.entities.LlmProvider`) without
either layer growing knowledge of the other:

* **Id namespacing.** A provider model's *public* id is
  ``provider:{provider_id}:{raw_model_id}`` (e.g.
  ``provider:3f...:openai/gpt-4o``). The ``provider:`` prefix is how every layer
  tells a provider model from a config-registry id (which keeps its existing
  shape, unchanged) — the surfacing layer builds it, the allow-list check and the
  chat runtime parse it. ``raw_model_id`` is the id sent upstream (it may itself
  contain colons, so parsing splits on the first two only).
* **Surfacing** (``GET /models``): :func:`provider_model_infos` maps every ENABLED
  provider's ``discovered_models`` to a picker entry with the namespaced id, a
  ``{model} · {provider}`` label, and ``provider`` set to the provider name.
* **Routing** (chat send path): :func:`resolve_provider_model` loads the provider
  (must be in the tenant + enabled), confirms the raw id is in its discovered
  snapshot, and returns the provider's ``base_url`` + ``api_key_secret_ref`` so the
  caller can decrypt the key once and route the completion through it.

Layering (ADR-0004): pure id helpers here + a thin repository read; no I/O beyond
the tenant-scoped :class:`~app.db.repositories.LlmProviderRepository`. Secret
decryption stays in the caller (the chat runtime), which holds the CC-C
:class:`~app.services.secrets_service.SecretsService` — this module never touches
a plaintext key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.repositories import AuditEventRepository, LlmProviderRepository
from app.domain.audit import AuditActor
from app.domain.entities import LlmProvider, Role
from app.domain.models import ChatModel, ModelTier
from app.services.audit import AuditSink
from app.services.secrets_service import build_secrets_service

# The prefix that marks a public model id as a per-tenant provider model rather
# than a config-registry id. A config id never starts with this (they are
# provider-qualified LiteLLM ids like ``openrouter/anthropic/…``).
PROVIDER_ID_PREFIX = "provider:"

# Provider models have no #47 tier (that grouping is a curated-registry concept);
# the contract enum offers only frontier/fast/oss, so they surface under
# ``frontier`` — the picker still groups them under their provider name via the
# ``provider`` field + label.
_PROVIDER_MODEL_TIER = ModelTier.FRONTIER


@dataclass(frozen=True, slots=True)
class ResolvedProviderModel:
    """A parsed + validated provider-model id ready for routing (chat send path).

    ``raw_model_id`` is the id to send upstream (never the namespaced public id);
    ``base_url`` is the provider's OpenAI-compatible API root; ``api_key_secret_ref``
    is the CC-C secret id the caller decrypts once per turn (``None`` for an
    anonymous provider — routed with no key override).
    """

    provider_id: UUID
    raw_model_id: str
    base_url: str
    api_key_secret_ref: str | None


def is_provider_model_id(model_id: str) -> bool:
    """Whether ``model_id`` is a namespaced per-tenant provider model id.

    ``True`` iff it carries the ``provider:`` prefix; a config-registry id (the
    unchanged built-in ids) is always ``False``. This is the single discriminator
    the allow-list check and the chat runtime branch on.
    """
    return model_id.startswith(PROVIDER_ID_PREFIX)


def make_provider_model_id(provider_id: UUID, raw_model_id: str) -> str:
    """Build the public namespaced id for a provider's discovered model."""
    return f"{PROVIDER_ID_PREFIX}{provider_id}:{raw_model_id}"


def parse_provider_model_id(model_id: str) -> tuple[UUID, str] | None:
    """Parse ``provider:{provider_id}:{raw_model_id}`` → ``(provider_id, raw_model_id)``.

    Returns ``None`` for a non-provider id or a malformed one (bad UUID / missing
    raw id) — the caller treats that as an unknown model (422). Splits on the
    first two colons only, so a ``raw_model_id`` that itself contains colons
    survives intact.
    """
    if not is_provider_model_id(model_id):
        return None
    remainder = model_id[len(PROVIDER_ID_PREFIX) :]
    provider_part, sep, raw_model_id = remainder.partition(":")
    if not sep or not raw_model_id:
        return None
    try:
        provider_id = UUID(provider_part)
    except ValueError:
        return None
    return provider_id, raw_model_id


def _raw_model_ids(provider: LlmProvider) -> set[str]:
    """The set of raw model ids in a provider's discovered snapshot."""
    ids: set[str] = set()
    for entry in provider.discovered_models:
        raw = entry.get("id")
        if isinstance(raw, str) and raw:
            ids.add(raw)
    return ids


def provider_model_infos(provider: LlmProvider) -> list[ChatModel]:
    """Project one ENABLED provider's discovered models to picker entries.

    Each discovered ``{"id", "label"}`` maps to a :class:`ChatModel` with the
    namespaced id, a ``{model} · {provider name}`` label, ``provider`` set to the
    provider's name, and a placeholder ``frontier`` tier. A disabled provider
    contributes nothing (the caller filters), so this always emits the models.
    """
    infos: list[ChatModel] = []
    for entry in provider.discovered_models:
        raw = entry.get("id")
        if not isinstance(raw, str) or not raw:
            continue
        raw_label = entry.get("label")
        model_label = raw_label if isinstance(raw_label, str) and raw_label else raw
        infos.append(
            ChatModel(
                id=make_provider_model_id(provider.id, raw),
                label=f"{model_label} · {provider.name}",
                provider=provider.name,
                tier=_PROVIDER_MODEL_TIER,
                is_default=False,
                description=None,
            )
        )
    return infos


async def list_provider_models(
    providers: LlmProviderRepository,
) -> list[ChatModel]:
    """Every ENABLED tenant provider's discovered models as picker entries.

    Reads the tenant's providers via the (already tenant-scoped) repository —
    INV-1: only this tenant's rows are ever seen. A disabled provider is skipped
    entirely, so its models never appear in the picker.
    """
    out: list[ChatModel] = []
    for provider in await providers.list_for_tenant():
        if not provider.enabled:
            continue
        out.extend(provider_model_infos(provider))
    return out


async def resolve_provider_model(
    model_id: str, providers: LlmProviderRepository
) -> ResolvedProviderModel | None:
    """Resolve a namespaced provider-model id to its routing inputs, or ``None``.

    ``None`` (⇒ the caller rejects the model, 422) when the id is malformed, the
    provider is not in this tenant (the repository sees no row — INV-1), the
    provider is disabled, or the raw model id is absent from its discovered
    snapshot. On success returns the raw model id + the provider's ``base_url`` +
    ``api_key_secret_ref`` (the key is decrypted by the caller, never here).
    """
    parsed = parse_provider_model_id(model_id)
    if parsed is None:
        return None
    provider_id, raw_model_id = parsed
    provider = await providers.get(provider_id)
    if provider is None or not provider.enabled:
        return None
    if raw_model_id not in _raw_model_ids(provider):
        return None
    return ResolvedProviderModel(
        provider_id=provider_id,
        raw_model_id=raw_model_id,
        base_url=provider.base_url,
        api_key_secret_ref=provider.api_key_secret_ref,
    )


async def is_allowed_provider_model(
    model_id: str, providers: LlmProviderRepository
) -> bool:
    """Whether a namespaced provider-model id is valid for this tenant (allow-list).

    ``True`` iff :func:`resolve_provider_model` resolves it (provider in tenant +
    enabled + raw id present in its discovered snapshot). An unknown / disabled /
    cross-tenant provider id is ``False`` → the chat service raises the 422.
    """
    return (await resolve_provider_model(model_id, providers)) is not None


# --- Chat routing: resolve a model id to its gateway credentials (PR 2a) ----


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The resolved routing inputs for one answer's chosen model (PR 2a).

    ``model`` is the id sent to the gateway: the RAW upstream id for a per-tenant
    provider model (the ``provider:`` prefix stripped), or the unchanged config id
    for a built-in. ``api_key`` / ``api_base`` OVERRIDE the gateway defaults for a
    provider model (the decrypted key + the provider's base URL), and are ``None``
    for a config model (default credentials, unchanged). Resolved ONCE per answer
    and reused across every gateway call of the turn loop — the key is decrypted
    once, never per call, never logged.
    """

    model: str
    api_key: str | None = None
    api_base: str | None = None


# The per-tenant model-route resolver the chat runtime calls ONCE per answer: given
# the runtime's own DB session + the chosen public model id, return the
# :class:`ModelRoute` to route the completion through. The live factory below
# decrypts a provider's key here (in a service, never in ``api/``); an offline
# runtime is given ``None`` and routes every id as-is.
ModelRouteResolver = Callable[[AsyncSession, str], Awaitable[ModelRoute]]


def build_model_route_resolver(
    *,
    settings: Settings,
    tenant_id: UUID,
    owner_id: UUID,
    roles: tuple[Role, ...],
    request_id: str,
    source_ip: str,
) -> ModelRouteResolver:
    """Assemble the live per-tenant model-route resolver (the production wiring).

    Keeps the CC-C secrets vault OUT of ``api/`` (the architecture test forbids a
    router importing the secrets service / naming ``get_secret_plaintext``): the
    router builds this resolver from settings + the streaming principal and hands it
    to the runtime; the plaintext decrypt happens here, in ``services/``. For a
    per-tenant ``provider:`` id the resolver loads the provider from the tenant
    (INV-1 — tenant-scoped repository), confirms it is enabled + lists the raw id,
    then decrypts its API key ONCE via the vault (a ``secret.accessed`` audit
    records the read, never the value) and returns the RAW model id + that
    provider's api_key/base_url. A config id — or a provider id that no longer
    resolves (deleted/disabled since the send-time 422 check) — is a plain
    passthrough with default credentials. The decrypted key rides the returned
    :class:`ModelRoute` for this answer only.
    """

    async def _resolver(session: AsyncSession, model: str) -> ModelRoute:
        providers = LlmProviderRepository(session, tenant_id)
        resolved = await resolve_provider_model(model, providers)
        if resolved is None:
            return ModelRoute(model=model)
        api_key: str | None = None
        if resolved.api_key_secret_ref is not None:
            audit = AuditSink(AuditEventRepository(session, tenant_id))
            secrets = build_secrets_service(
                session,
                settings=settings,
                tenant_id=tenant_id,
                owner_id=owner_id,
                roles=roles,
                audit=audit,
                request_id=request_id,
                source_ip=source_ip,
            )
            api_key = await secrets.get_secret_plaintext(
                UUID(resolved.api_key_secret_ref), accessor=AuditActor.system()
            )
        return ModelRoute(model=resolved.raw_model_id, api_key=api_key, api_base=resolved.base_url)

    return _resolver


__all__ = [
    "PROVIDER_ID_PREFIX",
    "ModelRoute",
    "ModelRouteResolver",
    "ResolvedProviderModel",
    "build_model_route_resolver",
    "is_allowed_provider_model",
    "is_provider_model_id",
    "list_provider_models",
    "make_provider_model_id",
    "parse_provider_model_id",
    "provider_model_infos",
    "resolve_provider_model",
]
