"""Write-mode discipline + the §3 cascade — for every ACL-declaring connector.

ADR-0019 §2's "the mode is never defaulted at write time" and §3's active
container-cascade invalidation, driven through the **real** sync task with only
the outside world faked (see :mod:`tests.acl_kit.sync_harness`).

The corpus auditor :func:`assert_all_managed_documents_are_enforced` is the
issue's write-mode assertion in reusable form: *a managed-source document
persisted with ``acl_enforced=false`` fails*. It is proven non-vacuous by
planting exactly that violation.

Subsumes, from ``tests/test_gdrive_sync_task.py`` (#453), the ACL-semantics
proofs: the write-seam/model default refusals, the attested-only snapshot, the
scope-cascade and ``integrity=incomplete`` stamps, the sticky-requirement
regression, and source-side revocation. The cursor/page/object/sweep mechanics
stay there — they prove sync plumbing, not deny-by-default.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.db.session as db_session
from app.connectors.base import AclMappingContext, ConnectorRun, PageIntegrity
from app.db import models
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role, SourceStatus
from app.retrieval import queries
from app.retrieval.permissions import AllowSet
from app.search.filters import SearchAllowFilter
from app.search.store import OpenSearchStore

from .engine import FakeEngine
from .subject import UNPROVABLE_CAUSE_IDS, AclSubject
from .subjects import SUBJECT_IDS, SUBJECTS
from .sync_harness import (
    DIM,
    FakeGateway,
    FakeObjectStore,
    KitConnector,
    PageSpec,
    engine_bound_store_factory,
    run_sync,
    settings,
    sync_source_module,
)

pytestmark = pytest.mark.parametrize("subject", SUBJECTS, ids=SUBJECT_IDS)

_ENGINE_URL = "http://engine.invalid"


class Seeded:
    """A tenant with a connected managed source, ready to sync."""

    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    collection_id: uuid.UUID
    source_id: uuid.UUID
    users: dict[str, uuid.UUID]


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def connector(
    subject: AclSubject, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> KitConnector:
    """Serve the kit connector at the framework's lookup seam.

    ``app.tasks.sync_source`` is re-exported on ``app.tasks`` as the Celery task
    object, so the module has to come from the import system (patching the
    string path would hit the task, not the module).
    """
    fake = KitConnector(subject)
    monkeypatch.setattr(sync_source_module, "get_connector", lambda _type: fake)
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", engine_bound_store_factory(engine))
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *a, **k: None)
    return fake


async def _seed(subject: AclSubject, *, attest_all_declared: bool = True) -> Seeded:
    seeded = Seeded()
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        users = UserRepository(session, tenant.id)
        created: dict[str, uuid.UUID] = {}
        for email in subject.tenant_users:
            role = Role.ADMIN if email == subject.attested_email else Role.MEMBER
            user = await users.create(email=email, password_hash="h", roles=[role])
            created[email] = user.id
            if attest_all_declared and email in subject.context.email_to_user_id:
                await users.attest_email(user.id, attested_by=user.id)
        owner_id = created[subject.attested_email]
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner_id, name=f"{subject.name}: kit"
        )
        source = await SourceRepository(session, tenant.id).create(
            owner_id=owner_id,
            type=subject.name,
            config={"collection_id": str(collection.id)},
            status=SourceStatus.PENDING,
        )
        await session.commit()
    seeded.tenant_id = tenant.id
    seeded.owner_id = owner_id
    seeded.collection_id = collection.id
    seeded.source_id = source.id
    seeded.users = created
    return seeded


async def _rows(seeded: Seeded) -> dict[str, models.Document]:
    async with db_session.session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(models.Document).where(models.Document.source_id == seeded.source_id)
                )
            )
            .scalars()
            .all()
        )
    return {str(row.external_id): row for row in rows}


async def _source_row(seeded: Seeded) -> models.Source:
    async with db_session.session_scope() as session:
        return (
            await session.execute(select(models.Source).where(models.Source.id == seeded.source_id))
        ).scalar_one()


async def assert_all_managed_documents_are_enforced(seeded: Seeded) -> None:
    """THE write-mode assertion (ADR-0019 §2), as a reusable corpus audit.

    Every document a ``map_acl``-declaring connector produced must carry
    ``acl_enforced = true``. A single ``false`` row means connector content is
    being governed by the owner/grant legs — the escalation the exclusive split
    exists to prevent — so this fails loudly and names the row.
    """
    offenders = [
        row.filename for row in (await _rows(seeded)).values() if row.acl_enforced is not True
    ]
    assert (
        not offenders
    ), f"managed-source documents persisted with acl_enforced=false: {sorted(offenders)}"


def _acl_visible(seeded: Seeded, user_id: uuid.UUID) -> AllowSet:
    return AllowSet.for_user(tenant_id=seeded.tenant_id, user_id=user_id)


# --- the mandatory, no-default write mode ------------------------------------


async def test_full_sync_writes_every_document_acl_enforced(
    sqlite_db: None, subject: AclSubject, connector: KitConnector
) -> None:
    """Structural derivation: ``map_acl`` present ⇒ every row is enforced."""
    seeded = await _seed(subject)
    connector.full_docs = (
        ("listed", subject.case("direct_user").raw, ()),
        ("unmapped", subject.case("group_only").raw, ()),
        ("public", subject.case("public").raw, ()),
    )
    result = await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert result.status is SourceStatus.READY

    await assert_all_managed_documents_are_enforced(seeded)
    rows = await _rows(seeded)
    assert set(rows) == {"listed", "unmapped", "public"}
    assert rows["unmapped"].acl_principals == []  # ingested, retrievable by nobody
    assert rows["public"].acl_principals == ["tenant"]
    assert all(row.acl_synced_at is not None for row in rows.values())
    # The silent-deny volume is surfaced to the admin.
    assert (await _source_row(seeded)).unmapped_acl_count == 1


async def test_the_write_mode_audit_detects_a_violation(
    sqlite_db: None, subject: AclSubject, connector: KitConnector
) -> None:
    """Meta-proof: the audit above is not vacuous.

    Plants exactly the defect the ADR names — a managed-source document written
    ``acl_enforced=false`` — and asserts the assertion fires.
    """
    seeded = await _seed(subject)
    connector.full_docs = (("listed", subject.case("direct_user").raw, ()),)
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    async with db_session.session_scope() as session:
        await DocumentRepository(session, seeded.tenant_id).create(
            owner_id=seeded.owner_id,
            collection_id=seeded.collection_id,
            filename="smuggled.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key=f"{seeded.tenant_id}/smuggled",
            acl_enforced=False,  # the defect
            status=DocumentStatus.READY,
            source_id=seeded.source_id,
            external_id="smuggled",
        )
        await session.commit()

    with pytest.raises(AssertionError, match="acl_enforced=false"):
        await assert_all_managed_documents_are_enforced(seeded)


async def test_write_seam_refuses_a_defaulted_acl_mode(
    sqlite_db: None, subject: AclSubject
) -> None:
    """The repository's ACL-mode argument is mandatory — no default to fall to."""
    seeded = await _seed(subject)
    async with db_session.session_scope() as session:
        with pytest.raises(TypeError):
            await DocumentRepository(session, seeded.tenant_id).create(  # type: ignore[call-arg]
                owner_id=seeded.owner_id,
                collection_id=seeded.collection_id,
                filename="x.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="t/x",
            )


async def test_model_insert_cannot_omit_the_acl_mode(sqlite_db: None, subject: AclSubject) -> None:
    """The mode has NO default at ANY layer — the column is NOT NULL, no server
    default, so a raw model insert that forgets it fails at the database."""
    seeded = await _seed(subject)
    with pytest.raises(IntegrityError):
        async with db_session.session_scope() as session:
            session.add(
                models.Document(
                    tenant_id=seeded.tenant_id,
                    owner_id=seeded.owner_id,
                    collection_id=seeded.collection_id,
                    filename="no-mode.txt",
                    mime_type="text/plain",
                    size_bytes=1,
                    storage_key="t/no-mode",
                    status="pending",
                )
            )
            await session.flush()


# --- the attested-identity snapshot ------------------------------------------


async def test_the_run_context_carries_attested_identities_only(
    sqlite_db: None, subject: AclSubject, connector: KitConnector
) -> None:
    """The framework freezes ONLY attested emails into the run (ADR-0019 §2/§4)."""
    seeded = await _seed(subject)
    connector.full_docs = (("listed", subject.case("direct_user").raw, ()),)
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    assert connector.seen_context is not None
    assert set(connector.seen_context.email_to_user_id) == set(subject.context.email_to_user_id)
    assert subject.unattested_email not in connector.seen_context.email_to_user_id


async def test_attestation_lights_up_the_document_on_the_next_sync(
    sqlite_db: None, subject: AclSubject, connector: KitConnector
) -> None:
    """The ADR's promise, end to end: nothing while unattested; visible after.

    The **source payload never changes** — only the identity attestation does.
    Note the intermediate step: a replay that does not re-examine the document
    leaves it denied, because ``acl_synced_at`` (and the mirror behind it)
    advance only for documents a run actually looked at (ADR-0019 §2 refresh
    cadence). Attestation lights a document up on the next sync that examines
    it, not merely on the next sync.
    """
    seeded = await _seed(subject)
    raw = subject.single_user_acl(subject.unattested_email)
    connector.full_docs = (("shared", raw, ()),)
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    unattested_id = seeded.users[subject.unattested_email]
    assert (await _rows(seeded))["shared"].acl_principals == []
    async with db_session.session_scope() as session:
        assert (
            await queries.list_documents(
                session, allow_set=_acl_visible(seeded, unattested_id), k=50
            )
            == []
        )

    # A tenant admin attests the identity.
    async with db_session.session_scope() as session:
        await UserRepository(session, seeded.tenant_id).attest_email(
            unattested_id, attested_by=seeded.owner_id
        )
        await session.commit()

    # A replay that examines nothing changes nothing — still denied.
    connector.pending_pages = {"cursor-1": []}
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert (await _rows(seeded))["shared"].acl_principals == []

    # The next replay that DOES re-examine it re-maps the identical payload
    # against the widened snapshot — and the user can now retrieve it.
    connector.pending_pages = {
        "cursor-1": [PageSpec(next_cursor="cursor-2", upserts=(("shared", raw, ()),))]
    }
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    assert (await _rows(seeded))["shared"].acl_principals == [f"user:{unattested_id}"]
    async with db_session.session_scope() as session:
        visible = await queries.list_documents(
            session, allow_set=_acl_visible(seeded, unattested_id), k=50
        )
    assert [row.document_id for row in visible] == [(await _rows(seeded))["shared"].id]


# --- §3 cascade: container change ⇒ descendants denied immediately -----------


async def test_container_change_denies_known_descendants_immediately(
    sqlite_db: None, subject: AclSubject, connector: KitConnector, engine: FakeEngine
) -> None:
    """A replayed container permission change stale-stamps every known
    descendant in the SAME page transaction — denied now, not at window expiry —
    and the stamp reaches the engine, while a re-examined document ends fresh."""
    seeded = await _seed(subject)
    public = subject.case("public").raw
    connector.full_docs = (
        ("inside", public, ("folderX",)),
        ("reexamined", public, ("folderX",)),
        ("outside", public, ("folderY",)),
    )
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert await _engine_visible(seeded, engine) == {"inside", "reexamined", "outside"}

    connector.pending_pages = {
        "cursor-1": [
            PageSpec(
                next_cursor="cursor-2",
                upserts=(("reexamined", public, ("folderX",)),),
                stale_scope_ids=frozenset({"folderX"}),
            )
        ]
    }
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    rows = await _rows(seeded)
    assert rows["inside"].acl_synced_at is None  # denied immediately
    assert rows["reexamined"].acl_synced_at is not None  # refreshed in-run
    assert rows["outside"].acl_synced_at is not None  # a different scope chain
    # Both stores agree the stamped descendant is gone.
    member = seeded.users[subject.attested_email]
    async with db_session.session_scope() as session:
        visible = {
            row.document_name
            for row in await queries.list_documents(
                session, allow_set=_acl_visible(seeded, member), k=50
            )
        }
    assert not any(name.endswith("Doc inside.txt") for name in visible)
    assert "inside" not in await _engine_visible(seeded, engine)


# --- §3 cause → signal → contract, in that order -----------------------------
#
# The framework's reaction to `integrity=incomplete` is ONE contract, so handing
# it a pre-formed incomplete page proves nothing about whether a given cause ever
# produces one. The two halves are therefore proven separately: the probes drive
# each connector's REAL change-replay capability under a real failure condition
# (cause → signal), and the framework test below consumes the signal (signal →
# contract).


@pytest.mark.parametrize("cause", UNPROVABLE_CAUSE_IDS)
async def test_the_connector_emits_incomplete_for_each_unprovable_cause(
    subject: AclSubject, cause: str
) -> None:
    """CAUSE → SIGNAL: the connector itself raises ``integrity=incomplete``.

    ``enumeration_failure`` and ``budget_exhausted`` are distinct ADR-0019 §3
    bullets with distinct code paths; neither is observable downstream unless the
    connector actually emits the signal. Budget exhaustion in particular had no
    producer-side coverage anywhere before this test.
    """
    probe = subject.probe(cause)
    pages = await probe.induce()
    assert pages, f"{subject.name}:{cause} induced no pages ({probe.why})"
    assert any(page.integrity is PageIntegrity.INCOMPLETE for page in pages), (
        f"{subject.name}: {probe.why} did NOT produce integrity=incomplete — "
        "the framework's source-wide denial can never fire for this cause"
    )


async def test_a_provable_cascade_stays_complete(subject: AclSubject) -> None:
    """The control that stops the probes above from being vacuous.

    A connector hard-coding ``INCOMPLETE`` would pass every cause probe while
    being useless (permanently source-wide denied). An in-budget, fully
    enumerable cascade must therefore report COMPLETE.
    """
    pages = await subject.probe("healthy_cascade").induce()
    assert pages
    assert all(
        page.integrity is PageIntegrity.COMPLETE for page in pages
    ), f"{subject.name} reports incomplete even for a provable cascade"


async def test_an_incomplete_page_fails_the_source_closed(
    sqlite_db: None,
    subject: AclSubject,
    connector: KitConnector,
    engine: FakeEngine,
) -> None:
    """SIGNAL → CONTRACT: an unprovable page stale-stamps every mirrored document.

    The framework does not guess which descendants a cascade touched — it denies
    the whole source, holds the cursor, and records the durable full-resync
    requirement.
    """
    seeded = await _seed(subject)
    public = subject.case("public").raw
    connector.full_docs = (("a", public, ("s1",)), ("b", public, ("s2",)))
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert await _engine_visible(seeded, engine) == {"a", "b"}

    connector.pending_pages = {
        "cursor-1": [PageSpec(next_cursor="cursor-2", integrity=PageIntegrity.INCOMPLETE)]
    }
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    rows = await _rows(seeded)
    assert rows["a"].acl_synced_at is None
    assert rows["b"].acl_synced_at is None
    assert await _engine_visible(seeded, engine) == set()
    source = await _source_row(seeded)
    assert source.acl_resync_required is True
    assert source.sync_cursor == "cursor-1"  # an incomplete page is never consumed


async def test_a_healthy_terminal_page_cannot_publish_over_a_stale_stamped_row(
    sqlite_db: None, subject: AclSubject, connector: KitConnector
) -> None:
    """A complete retry re-examines a *subset*; it can never clear a source-wide
    stamp. Publishing READY + fresh health over rows still sitting at NULL would
    report healthy while the corpus is invisible."""
    seeded = await _seed(subject)
    public = subject.case("public").raw
    connector.full_docs = (("reported", public, ()), ("unreported", public, ()))
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    connector.pending_pages = {
        "cursor-1": [PageSpec(next_cursor="cursor-2", integrity=PageIntegrity.INCOMPLETE)]
    }
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    stamped = await _rows(seeded)
    assert stamped["reported"].acl_synced_at is None
    assert stamped["unreported"].acl_synced_at is None

    # The page now replays cleanly, but reports only ONE of the two rows.
    connector.pending_pages = {
        "cursor-1": [PageSpec(next_cursor="cursor-3", upserts=(("reported", public, ()),))]
    }
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, seeded.tenant_id).get(seeded.source_id)
    assert source is not None
    run = _null_run()
    try:
        result = await sync_source_module._sync_incremental(
            seeded.tenant_id,
            source,
            connector,
            run,
            settings=settings(),
            object_store=FakeObjectStore(),
            gateway=FakeGateway(),
            collection_id=seeded.collection_id,
            enforce_acl=True,
        )
    finally:
        await run.http.aclose()

    after = await _rows(seeded)
    assert after["reported"].acl_synced_at is not None  # genuinely re-examined
    assert after["unreported"].acl_synced_at is None  # STILL denied
    row = await _source_row(seeded)
    assert row.acl_resync_required is True
    assert row.acl_synced_at is None  # no fresh source-level health
    assert row.status == "error"
    assert result.status is SourceStatus.ERROR
    assert result.error == "acl_mirror_incomplete"  # the reason is recorded, not generic

    # ...and the caller refuses the incremental path entirely while the
    # requirement stands: the next run is a FULL replay, not another page.
    calls_before = connector.sync_calls
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())
    assert connector.sync_calls == calls_before + 1


# --- source-side revocation ---------------------------------------------------


async def test_source_side_revocation_is_enforced_after_the_next_sync(
    sqlite_db: None, subject: AclSubject, connector: KitConnector, engine: FakeEngine
) -> None:
    """A revoked source ACL vanishes from the mirror on the next replay: the
    document survives, and admits nobody (INV-2 — excluded after next sync)."""
    seeded = await _seed(subject)
    connector.full_docs = (("d1", subject.case("direct_user").raw, ()),)
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    listed = seeded.users[subject.attested_email]
    assert (await _rows(seeded))["d1"].acl_principals == [f"user:{listed}"]
    assert await _engine_visible(seeded, engine) == {"d1"}

    connector.pending_pages = {
        "cursor-1": [
            PageSpec(
                next_cursor="cursor-2",
                upserts=(("d1", subject.case("revoked_entry").raw, ()),),
            )
        ]
    }
    await run_sync(seeded.tenant_id, seeded.source_id, object_store=FakeObjectStore())

    after = await _rows(seeded)
    assert after["d1"].acl_principals == []  # the mirror lost the principal
    assert after["d1"].acl_synced_at is not None  # ...and it IS fresh: a real deny
    assert await _engine_visible(seeded, engine) == set()
    async with db_session.session_scope() as session:
        assert (
            await queries.list_documents(session, allow_set=_acl_visible(seeded, listed), k=50)
            == []
        )
    assert (await _source_row(seeded)).unmapped_acl_count == 1


# --- helpers ------------------------------------------------------------------


async def _engine_visible(seeded: Seeded, engine: FakeEngine) -> set[str]:
    """Document filenames (sans suffix) the REAL engine query returns.

    Uses the connecting admin's own filter widened with the tenant principal —
    the most permissive requester in the tenant — so anything still reachable
    shows up. Whatever this set omits is denied to *everyone*.
    """
    rows = await _rows(seeded)
    by_id = {str(row.id): external_id for external_id, row in rows.items()}
    owner_allow = AllowSet.for_user(tenant_id=seeded.tenant_id, user_id=seeded.owner_id)
    allow = SearchAllowFilter(
        tenant_id=seeded.tenant_id,
        owner_ids=frozenset({seeded.owner_id}),
        acl_principals=owner_allow.acl_principals
        | frozenset(f"user:{uid}" for uid in seeded.users.values()),
    )
    store = OpenSearchStore(
        base_url=_ENGINE_URL,
        index=engine.index,
        dimensions=DIM,
        client=httpx.AsyncClient(
            base_url=_ENGINE_URL, transport=httpx.MockTransport(engine.handler)
        ),
    )
    try:
        hits = await store.hybrid_search(
            query_text="fox", embedding=[0.1] * DIM, allow=allow, k=100
        )
    finally:
        await store.aclose()
    return {by_id[str(hit.document_id)] for hit in hits if str(hit.document_id) in by_id}


def _null_run() -> ConnectorRun:
    """A framework run context for the direct ``_sync_incremental`` drive."""
    return ConnectorRun(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
        ),
        acl_context=AclMappingContext(email_to_user_id={}, evaluated_at=datetime.now(UTC)),
    )
