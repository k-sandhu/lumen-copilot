"""Domain value objects for the chat-model picker registry (issue #47).

Pure, frozen dataclasses — **no I/O, no framework imports** (backend/AGENTS.md:
``domain/`` is pure). These are the in-code shape of an entry in the curated
model registry the picker offers; the wire shape (``ChatModelInfo`` in
``contracts/openapi.yaml``) is a projection of :class:`ChatModel`, mapped in the
router. Keeping the registry as domain types means the config setting, the
service helper, and the response all agree on one shape.

The registry itself is **config** (``core/config.py``), not data hardcoded here:
adding or removing a model is a config change, never a code change (AC-2).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ModelTier(str, enum.Enum):
    """Picker grouping (mirrors ``ModelTier`` in ``contracts/openapi.yaml``).

    ``frontier`` = highest-quality flagship models; ``fast`` = lower-latency /
    cheaper models; ``oss`` = open-weight models. The UI groups the registry by
    this tier.
    """

    FRONTIER = "frontier"
    FAST = "fast"
    OSS = "oss"


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One selectable chat model in the curated registry (issue #47).

    Mirrors a row of ``ChatModelInfo`` (``contracts/openapi.yaml``): ``id`` is
    the provider-qualified id passed to the chat runtime (e.g.
    ``anthropic/claude-opus-4.8``); ``label`` is the picker display name;
    ``provider`` is the vendor (anthropic/openai/google/…); ``tier`` is the
    picker grouping; ``is_default`` marks the single registry default; the
    optional ``description`` is a short hint for the UI.
    """

    id: str
    label: str
    provider: str
    tier: ModelTier
    is_default: bool = False
    description: str | None = None
