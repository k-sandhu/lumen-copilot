"""Offline metadata narrowing: slash boundaries, unknown facets and ACL precedence."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.classification import ClassificationFilter, ClassificationMetadata
from app.search.filters import SearchAllowFilter
from app.search.store import _hybrid_body, _index_body
from tests import test_retrieval_service as retrieval_fixtures

session = retrieval_fixtures.session
two_tenants = retrieval_fixtures.two_tenants


def test_prefix_and_unknown_facets_are_narrow_only() -> None:
    metadata = ClassificationMetadata.from_result(
        "classified",
        {
            "path": "financial/statements/balance_sheet",
            "taxonomy_version": "1.0.0",
            "facets": {"has_tables": {"value": True}, "is_signed": {"value": None}},
            "method": "decisions",
            "confidence": 0.7,
        },
    )
    assert metadata is not None
    assert ClassificationFilter("financial").matches(metadata)
    assert not ClassificationFilter("finance").matches(metadata)
    assert ClassificationFilter(facets=(("has_tables", True),)).matches(metadata)
    assert not ClassificationFilter(facets=(("is_signed", False),)).matches(metadata)
    assert not ClassificationFilter("financial").matches(None)
    assert ClassificationMetadata.from_result("pending", {"path": metadata.path}) is None
    assert ClassificationMetadata.from_result("incomplete", {"path": metadata.path}) is None
    assert (
        ClassificationMetadata.from_result(
            "override", {"path": metadata.path, "taxonomy_version": "1.0.0", "method": "override"}
        )
        is not None
    )


@pytest.mark.parametrize("prefix", ["financial/", "financial*", "../financial", "", "a/b/c/d/e"])
def test_malformed_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(ValueError):
        ClassificationFilter(prefix)


def test_duplicate_conflicting_facets_rejected() -> None:
    with pytest.raises(ValueError):
        ClassificationFilter(facets=(("has_tables", True), ("has_tables", False)))


def test_hybrid_metadata_filters_follow_permissions_in_both_legs(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.search.filters.acl_freshness_floor", lambda now=None: datetime(2026, 10, 8, tzinfo=UTC)
    )
    tenant, owner = uuid4(), uuid4()
    allow = SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({owner}))
    body = _hybrid_body(
        query_text="revenue",
        embedding=[0.0],
        embedding_fingerprint="f" * 64,
        allow=allow,
        k=10,
        classification=ClassificationFilter("financial", (("has_tables", True),)),
    )
    legs = body["query"]["hybrid"]["queries"]
    lexical = legs[0]["bool"]["filter"]
    vector = legs[1]["knn"]["embedding"]["filter"]["bool"]["filter"]
    assert lexical == vector
    assert lexical[:2] == list(allow.to_engine_filter())
    assert {"term": {"classification_ancestors": "financial"}} in lexical
    assert {"term": {"classification_facets": "has_tables:true"}} in lexical
    assert "classification" not in body["_source"]
    properties = _index_body(1, "f" * 64)["mappings"]["properties"]
    assert properties["classification_ancestors"]["type"] == "keyword"
    assert properties["classification_facets"]["type"] == "keyword"


async def test_current_metadata_rechecked_only_after_permission_hydration(session, two_tenants):
    """A malicious/stale engine cannot bypass tenant, owner or current type checks."""
    from app.db import models
    from app.db.classification import ClassificationRepository
    from app.db.repositories import ChunkRepository
    from app.retrieval import RetrievalService
    from app.search import SearchHit

    tenant, foreign = two_tenants
    visible_user, visible_doc = await retrieval_fixtures._seed_user_with_document(
        session,
        tenant_id=tenant,
        email="reader@synthetic.test",
        filename="visible.txt",
        chunk_texts=["visible"],
    )
    _, hidden_doc = await retrieval_fixtures._seed_user_with_document(
        session,
        tenant_id=tenant,
        email="hidden@synthetic.test",
        filename="hidden.txt",
        chunk_texts=["hidden"],
    )
    _, foreign_doc = await retrieval_fixtures._seed_user_with_document(
        session,
        tenant_id=foreign,
        email="foreign@synthetic.test",
        filename="foreign.txt",
        chunk_texts=["foreign"],
    )
    hits = []
    for tid, doc in [(tenant, visible_doc), (tenant, hidden_doc), (foreign, foreign_doc)]:
        session.add(
            models.DocumentClassification(
                tenant_id=tid,
                document_id=doc,
                input_fingerprint="a" * 64,
                extraction_id="b" * 64,
                taxonomy_version="1.0.0",
                input_json="{}",
                status="classified",
                result={
                    "path": "financial/statements/balance_sheet",
                    "taxonomy_version": "1.0.0",
                    "facets": {},
                },
            )
        )
        chunks = await ChunkRepository(session, tid).list_for_document(doc)
        hits.append(SearchHit(chunks[0].id, doc, 0.9, 0, retrieval_fixtures._TEST_FP))
    await session.flush()

    class Store:
        async def ensure_index(self):
            pass

        async def hybrid_search(self, **kwargs):
            assert kwargs["allow"].tenant_id == tenant
            assert kwargs["classification"] == ClassificationFilter("financial")
            return hits

    svc = RetrievalService(session, gateway=retrieval_fixtures._FakeGateway(), store=Store())
    principal = retrieval_fixtures._principal(visible_user, tenant)
    result = await svc.search(
        principal=principal, query="balance", k=10, classification=ClassificationFilter("financial")
    )
    assert [value.document_id for value in result] == [visible_doc]
    assert foreign_doc not in await ClassificationRepository(
        session, tenant
    ).metadata_for_documents([foreign_doc])
    # Current override no longer matches stale financial metadata in the engine.
    repo = ClassificationRepository(session, tenant)
    work = await repo.get(visible_doc)
    await repo.override(
        visible_doc,
        expected_revision=work.revision,
        path="other/other/other",
        actor=visible_user,
        reason="synthetic correction",
        taxonomy_version="1.0.0",
    )
    assert (
        await svc.search(
            principal=principal,
            query="balance",
            k=10,
            classification=ClassificationFilter("financial"),
        )
        == []
    )


async def test_unknown_identifier_rejected_without_engine_query(session):
    from app.core.errors import ValidationError
    from app.retrieval import RetrievalService

    svc = RetrievalService(session, gateway=retrieval_fixtures._FakeGateway())
    with pytest.raises(ValidationError):
        await svc.search(
            principal=retrieval_fixtures._principal(uuid4(), uuid4()),
            query="x",
            k=10,
            classification=ClassificationFilter("made_up"),
        )
