"""Focused unit tests for answer-local evidence handles and citation selection."""

from __future__ import annotations

import re
import uuid

from app.domain.retrieval import RetrievedPassage


def _passage(text: str = "A permitted passage.") -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        document_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        document_name="facts.txt",
        ord=0,
        text=text,
        char_start=0,
        char_end=len(text),
        score=0.9,
    )


def test_kinds_share_one_monotonic_allocator() -> None:
    from app.services.tools.handles import EvidenceHandles

    handles = EvidenceHandles()
    source = handles.passage(_passage())
    document = handles.document(uuid.UUID("00000000-0000-0000-0000-000000000003"))
    web = handles.web("https://example.com/a", "Page A", "Useful text")

    assert (source, document, web) == ("S1", "D2", "W3")
    assert list(handles.entries) == ["S1", "D2", "W3"]
    assert [handles.entries[key]["kind"] for key in (source, document, web)] == [
        "passage",
        "document",
        "web",
    ]
    assert list(handles.created) == ["S1", "D2", "W3"]


def test_same_evidence_reuses_its_handle_but_changed_passage_text_does_not() -> None:
    from app.services.tools.handles import EvidenceHandles

    handles = EvidenceHandles()
    first = handles.passage(_passage("alpha"))
    repeated = handles.passage(_passage("alpha"))
    changed = handles.passage(_passage("beta"))

    assert first == repeated == "S1"
    assert changed == "S2"
    assert list(handles.entries) == ["S1", "S2"]


def test_document_and_web_handles_are_stable_for_repeated_evidence() -> None:
    from app.services.tools.handles import EvidenceHandles

    handles = EvidenceHandles()
    document_id = uuid.UUID("00000000-0000-0000-0000-000000000004")

    assert handles.document(document_id) == "D1"
    assert handles.document(document_id) == "D1"
    assert handles.web("https://example.com/a", "Page A", "Useful text") == "W2"
    assert handles.web("https://example.com/a", "Page A", "Useful text") == "W2"
    assert list(handles.entries) == ["D1", "W2"]


def test_existing_conversation_handles_are_kept_and_allocation_continues() -> None:
    from app.services.tools.handles import EvidenceHandles

    prior = EvidenceHandles(first=4)
    old_source = prior.passage(_passage("old passage"))
    old_web = prior.web("https://example.com/old", "Old page", "Old text")
    existing = prior.entries
    handles = EvidenceHandles(first=4, existing=existing)

    assert (old_source, old_web) == ("S4", "W5")
    assert handles.resolve(old_source) == existing[old_source]
    assert handles.resolve(old_web) == existing[old_web]
    assert handles.passage(_passage("old passage")) == old_source
    assert handles.passage(_passage()) == "S6"
    assert list(handles.entries) == [old_source, old_web, "S6"]
    assert list(handles.created) == ["S6"]


def test_unknown_handle_resolves_to_none() -> None:
    from app.services.tools.handles import EvidenceHandles

    assert EvidenceHandles().resolve("S999") is None


def test_capacity_exhaustion_raises_a_typed_validation_error() -> None:
    from app.core.errors import ValidationError
    from app.services.tools.handles import EvidenceHandles

    handles = EvidenceHandles(capacity=1)
    handles.passage(_passage("first"))

    try:
        handles.document(uuid.UUID("00000000-0000-0000-0000-000000000005"))
    except ValidationError:
        pass
    else:
        raise AssertionError("allocating past the handle capacity must be refused")


def test_citation_selection_keeps_available_source_and_web_handles_once_in_order() -> None:
    from app.services.tools.handles import select_cited_handles

    answer, cited = select_cited_handles(
        "See [W2], then [S1], then [W2] again. **Done.**",
        available={"S1", "W2"},
    )

    assert answer == "See [W2], then [S1], then [W2] again. **Done.**"
    assert cited == ["W2", "S1"]


def test_unknown_handles_and_document_markers_are_stripped_not_selected() -> None:
    from app.services.tools.handles import select_cited_handles

    answer, cited = select_cited_handles(
        "Known [S1], unknown [S99], document [D2], and [W4].",
        available={"S1", "D2", "W4"},
    )

    assert answer == "Known [S1], unknown , document , and [W4]."
    assert cited == ["S1", "W4"]
    assert not re.search(r"\[(?:D[1-9][0-9]*|S99)\]", answer)


def test_uncited_refusal_returns_no_citations() -> None:
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK
    from app.services.tools.handles import select_cited_handles

    answer, cited = select_cited_handles(NO_SOURCES_FALLBACK, available={"S1", "W1"})

    assert answer == NO_SOURCES_FALLBACK
    assert cited == []


def test_unrelated_markdown_is_preserved() -> None:
    from app.services.tools.handles import select_cited_handles

    answer, cited = select_cited_handles(
        "Use **bold**, `code`, [a normal link](https://example.com), and # a heading.",
        available=set(),
    )

    assert answer == "Use **bold**, `code`, [a normal link](https://example.com), and # a heading."
    assert cited == []


def test_citation_selection_ignores_markers_inside_code_and_markdown_links() -> None:
    from app.services.tools.handles import select_cited_handles

    answer = (
        "Literal `[S2]` and [marker link](https://example.test/[W8]).\n\n"
        "```text\n[S3]\n```\nThe claim is supported [S1]."
    )

    cleaned, selected = select_cited_handles(answer, {"S1", "S2", "S3", "W8"})

    assert cleaned == answer
    assert selected == ["S1"]
