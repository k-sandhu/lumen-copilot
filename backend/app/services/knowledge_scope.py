"""Effective knowledge scope of a pinned assistant version (#581 part 2).

The ONE helper computing the scope a chat session is actually governed by,
from the session's PINNED assistant version's frozen config — never from the
assistant's mutable head and never from client state. The session wire
(``EffectiveKnowledgeScope`` in ``contracts/openapi.yaml``) carries this so
the UI shows the real, server-authoritative scope and it survives reload.

**Effective-modes rule** (owner decision for #581, shared with the
scope-server lane — keep identical): if the version's
``knowledgeScope.modes`` is EMPTY (legacy), the effective modes are
``company`` + ``uploaded``, plus ``web`` when ``web_search`` is in its tool
allowlist. If ``modes`` is NON-empty it is authoritative. The ``model`` mode
is never honoured (answers are always grounded), so it never appears here.
Users cannot narrow or widen scope per session in this change.

To reverse that decision: change :func:`effective_modes` and the
``adjustable`` flags on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.entities import AssistantVersion, KnowledgeMode

#: Canonical order effective modes are reported in (matches the UI order).
_MODE_ORDER: tuple[KnowledgeMode, ...] = (
    KnowledgeMode.COMPANY,
    KnowledgeMode.UPLOADED,
    KnowledgeMode.WEB,
    KnowledgeMode.MODEL,
)

_WEB_SEARCH_TOOL = "web_search"


@dataclass(frozen=True, slots=True)
class EffectiveKnowledgeScope:
    """The pinned version's governing scope, projected for the wire."""

    assistant_id: UUID
    assistant_version_id: UUID
    modes: tuple[KnowledgeMode, ...]
    collection_ids: tuple[UUID, ...]
    source_ids: tuple[UUID, ...]


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _scope_dict(config: dict[str, object]) -> dict[str, object]:
    scope = config.get("knowledgeScope")
    return scope if isinstance(scope, dict) else {}


def _declared_modes(config: dict[str, object]) -> list[KnowledgeMode]:
    """Valid declared modes; unknown values are dropped (scope only narrows)."""
    out: list[KnowledgeMode] = []
    for raw in _as_list(_scope_dict(config).get("modes")):
        try:
            mode = KnowledgeMode(str(raw))
        except ValueError:
            continue
        if mode not in out:
            out.append(mode)
    return out


def effective_modes(config: dict[str, object]) -> tuple[KnowledgeMode, ...]:
    """The effective modes for a frozen version ``config`` (the one helper).

    Non-empty declared ``modes`` are authoritative. Empty (legacy) falls back
    to company+uploaded, plus web iff ``web_search`` is in the tool
    allowlist. ``model`` is never honoured. Canonical order, deduplicated.
    """
    if _as_list(_scope_dict(config).get("modes")):
        # Decide legacy fallback before filtering: an explicit but unusable
        # declaration must never widen into the legacy corpus/web defaults.
        effective = set(_declared_modes(config))
    else:
        effective = {KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED}
        if _WEB_SEARCH_TOOL in [str(t) for t in _as_list(config.get("toolAllowlist"))]:
            effective.add(KnowledgeMode.WEB)
    effective.discard(KnowledgeMode.MODEL)
    return tuple(mode for mode in _MODE_ORDER if mode in effective)


def _uuid_tuple(values: object) -> tuple[UUID, ...]:
    out: list[UUID] = []
    for raw in _as_list(values):
        try:
            value = UUID(str(raw))
        except (ValueError, TypeError):
            continue
        if value not in out:
            out.append(value)
    return tuple(out)


def effective_scope_from_version(
    *, assistant_id: UUID, version: AssistantVersion
) -> EffectiveKnowledgeScope:
    """Resolve the effective scope from one pinned version snapshot."""
    config = version.config
    scope = _scope_dict(config)
    return EffectiveKnowledgeScope(
        assistant_id=assistant_id,
        assistant_version_id=version.id,
        modes=effective_modes(config),
        collection_ids=_uuid_tuple(scope.get("collectionIds")),
        source_ids=_uuid_tuple(scope.get("sourceIds")),
    )


__all__ = [
    "EffectiveKnowledgeScope",
    "effective_modes",
    "effective_scope_from_version",
]
