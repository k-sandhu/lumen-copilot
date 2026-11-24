"""Effective knowledge scope from a pinned assistant version (#581 part 2).

The session wire must show the PINNED version's real scope. The owner
decision for this lane: empty ``modes`` (legacy) ⇒ company+uploaded, plus
web iff ``web_search`` is in the tool allowlist; non-empty ``modes`` is
authoritative; ``model`` is never honoured (answers are grounded).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.entities import AssistantVersion, KnowledgeMode
from app.services.knowledge_scope import (
    effective_modes,
    effective_scope_from_version,
)


def _version(config: dict[str, object]) -> AssistantVersion:
    return AssistantVersion(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        assistant_id=uuid.uuid4(),
        version=1,
        author_id=None,
        config=config,
        notes=None,
        diff_summary=None,
        created_at=datetime.now(UTC),
    )


def test_nonempty_modes_are_authoritative_and_model_is_never_honoured() -> None:
    config = {
        "knowledgeScope": {"modes": ["web", "model", "company"]},
        "toolAllowlist": [],
    }
    assert effective_modes(config) == (KnowledgeMode.COMPANY, KnowledgeMode.WEB)


def test_empty_modes_legacy_fallback_without_web_tool() -> None:
    config = {"knowledgeScope": {"modes": []}, "toolAllowlist": ["search_text"]}
    assert effective_modes(config) == (KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED)


def test_empty_modes_legacy_fallback_adds_web_when_web_search_allowlisted() -> None:
    config = {"knowledgeScope": {}, "toolAllowlist": ["web_search"]}
    assert effective_modes(config) == (
        KnowledgeMode.COMPANY,
        KnowledgeMode.UPLOADED,
        KnowledgeMode.WEB,
    )


def test_unknown_modes_are_dropped_and_duplicates_collapsed() -> None:
    config = {
        "knowledgeScope": {"modes": ["uploaded", "uploaded", "bogus"]},
        "toolAllowlist": ["web_search"],
    }
    # Non-empty modes are authoritative: web_search alone must NOT add web.
    assert effective_modes(config) == (KnowledgeMode.UPLOADED,)


@pytest.mark.parametrize("modes", [["model"], ["unknown"], ["unknown", "model"]])
def test_nonempty_unusable_modes_do_not_activate_legacy_defaults(modes: list[str]) -> None:
    config = {"knowledgeScope": {"modes": modes}, "toolAllowlist": ["web_search"]}
    assert effective_modes(config) == ()


def test_scope_carries_pinned_ids_and_collection_source_ids() -> None:
    collection = uuid.uuid4()
    source = uuid.uuid4()
    version = _version(
        {
            "knowledgeScope": {
                "collectionIds": [str(collection), "not-a-uuid", str(collection)],
                "sourceIds": [str(source)],
                "modes": ["company"],
            },
            "toolAllowlist": [],
        }
    )
    scope = effective_scope_from_version(assistant_id=version.assistant_id, version=version)
    assert scope.assistant_id == version.assistant_id
    assert scope.assistant_version_id == version.id
    assert scope.modes == (KnowledgeMode.COMPANY,)
    assert scope.collection_ids == (collection,)
    assert scope.source_ids == (source,)
