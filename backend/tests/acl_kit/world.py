"""The seeded two-store world every enforcement proof runs against.

One corpus, seeded **from the subject's own mapped principal sets** (never from
hand-written principal strings), materialised into both chokepoints:

* Postgres — in-memory SQLite through the real repositories and the real
  ``retrieval/queries`` builders;
* OpenSearch — the real ``index_sync`` projection + the real ``OpenSearchStore``
  writer/reader over :class:`tests.acl_kit.engine.FakeEngine`.

:func:`expected_visible` is a third, independent statement of the ADR-0019 §2
rule in plain Python. The three must agree: if the SQL and the engine filter
drifted apart they disagree with each other; if they drifted *together* they
disagree with the oracle.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

import app.db.session as db_session
from app.connectors.base import AclMappingContext
from app.db import models
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.retrieval.permissions import AllowSet
from app.search.filters import SearchAllowFilter, acl_freshness_floor
from app.search.store import OpenSearchStore
from app.tasks.index_sync import sync_document_index_async

from .engine import FakeEngine
from .subject import AclSubject

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

# Documents seeded on top of the subject's case fixtures. Keys are stable so the
# proofs can name them; every one is an ADR-0019 §2 bullet.
STALE_BEYOND_WINDOW = "stale_beyond_window"
STALE_STAMPED = "stale_stamped"
UPLOAD = "upload"
GRANTED_ENFORCED = "granted_enforced"
REVOKED_AT_SOURCE = "revoked_at_source"


@dataclass(frozen=True, slots=True)
class SeededDoc:
    """One seeded document, as the independent oracle sees it."""

    key: str
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    acl_enforced: bool
    principals: frozenset[str]
    fresh: bool
    granted_to: frozenset[uuid.UUID] = frozenset()


@dataclass
class World:
    """A seeded tenant + its foreign neighbour, in both stores."""

    subject: AclSubject
    tenant_id: uuid.UUID
    foreign_tenant_id: uuid.UUID
    collection_id: uuid.UUID
    owner_id: uuid.UUID
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    foreign_user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    docs: dict[str, SeededDoc] = field(default_factory=dict)
    engine: FakeEngine = field(default_factory=FakeEngine)
    # The framework's attested-identity snapshot for THIS seeded tenant — the
    # subject's declared uuids are fixture values; these are the real rows.
    context: AclMappingContext | None = None

    # --- identities -----------------------------------------------------------

    def user_id(self, email: str) -> uuid.UUID:
        return self.users[email]

    def allow_set(self, user_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None) -> AllowSet:
        """The Postgres allow-set the request path would build for this user."""
        return AllowSet.for_user(tenant_id=tenant_id or self.tenant_id, user_id=user_id)

    def search_filter(
        self, user_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None
    ) -> SearchAllowFilter:
        """The engine allow-filter for the SAME user — the lockstepped twin.

        Derived from the same :class:`AllowSet` the SQL side uses, including the
        resolved Lumen grants, so any divergence in the proofs is a divergence
        between the two *predicates*, never between two hand-built inputs.
        """
        allow = self.allow_set(user_id, tenant_id=tenant_id)
        granted = frozenset(
            doc.document_id for doc in self.docs.values() if user_id in doc.granted_to
        )
        return SearchAllowFilter(
            tenant_id=allow.tenant_id,
            owner_ids=allow.owner_ids,
            granted_document_ids=granted,
            acl_principals=allow.acl_principals,
        )

    def principals_of(self, user_id: uuid.UUID) -> frozenset[str]:
        return frozenset({f"user:{user_id}", "tenant"})

    # --- the independent oracle ----------------------------------------------

    def expected_visible(self, user_id: uuid.UUID) -> set[str]:
        """ADR-0019 §2's rule, restated in plain Python (the third opinion).

        ``acl_enforced`` ⇒ a fresh mirrored-principal intersection and nothing
        else (owner and grant legs do NOT apply). Otherwise ⇒ owner-or-grant.
        """
        mine = self.principals_of(user_id)
        visible: set[str] = set()
        for doc in self.docs.values():
            if doc.acl_enforced:
                if doc.fresh and (doc.principals & mine):
                    visible.add(doc.key)
            elif user_id == self.owner_id or user_id in doc.granted_to:
                visible.add(doc.key)
        return visible

    def key_of(self, document_id: uuid.UUID) -> str:
        return next(d.key for d in self.docs.values() if d.document_id == document_id)

    # --- the engine side ------------------------------------------------------

    def store(self) -> OpenSearchStore:
        """A real store bound to the in-memory engine (caller closes it)."""
        return OpenSearchStore(
            base_url=_ENGINE_URL,
            index=self.engine.index,
            dimensions=_DIM,
            client=httpx.AsyncClient(
                base_url=_ENGINE_URL, transport=httpx.MockTransport(self.engine.handler)
            ),
        )

    async def engine_visible(
        self, user_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None
    ) -> set[str]:
        """Document keys the REAL engine query surfaces for this user."""
        store = self.store()
        try:
            hits = await store.hybrid_search(
                query_text="anything",
                embedding=[0.1] * _DIM,
                allow=self.search_filter(user_id, tenant_id=tenant_id),
                k=100,
            )
        finally:
            await store.aclose()
        return {self.key_of(hit.document_id) for hit in hits}


_DIM = 8
_ENGINE_URL = "http://engine.invalid"
_TEXT = "The quick brown fox jumps over the lazy dog. " * 4


async def build_world(subject: AclSubject) -> World:
    """Seed the corpus for one subject into both stores.

    Every ``acl_enforced`` document's principal set comes from **that
    connector's own ``map_acl``** over its own fixture — and over an identity
    snapshot read out of the database with the production query
    (:meth:`UserRepository.attested_email_map`), so these proofs cover the whole
    chain "attested user row → framework snapshot → connector mapping → both
    chokepoints", not principal strings a test author typed.
    """
    engine = FakeEngine()
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        foreign = await TenantRepository(session).create(name="Globex")
        users = UserRepository(session, tenant.id)
        created: dict[str, uuid.UUID] = {}
        for email in subject.tenant_users:
            role = Role.ADMIN if email == subject.attested_email else Role.MEMBER
            user = await users.create(email=email, password_hash="h", roles=[role])
            created[email] = user.id
            # Only the subject's declared attested identities get an attestation.
            if email in subject.context.email_to_user_id:
                await users.attest_email(user.id, attested_by=user.id)
        foreign_user = await UserRepository(session, foreign.id).create(
            email="spy@globex.test", password_hash="h", roles=[Role.MEMBER]
        )
        owner_id = created[subject.attested_email]
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner_id, name=f"{subject.name}: kit"
        )
        await session.commit()

    # The framework's sync-time snapshot, built the way the sync task builds it.
    async with db_session.session_scope() as session:
        attested = await UserRepository(session, tenant.id).attested_email_map()
    context = AclMappingContext(
        email_to_user_id=attested, evaluated_at=subject.context.evaluated_at
    )

    world = World(
        subject=subject,
        tenant_id=tenant.id,
        foreign_tenant_id=foreign.id,
        collection_id=collection.id,
        owner_id=owner_id,
        users=created,
        foreign_user_id=foreign_user.id,
        engine=engine,
        context=context,
    )

    fresh = datetime.now(UTC)
    stale = acl_freshness_floor() - timedelta(hours=1)
    member_principal = frozenset({f"user:{created[subject.attested_email]}"})

    # One enforced document per ACL fixture, principals straight from map_acl.
    for case in subject.cases:
        await _seed_doc(
            world,
            key=f"case:{case.id}",
            acl_enforced=True,
            principals=subject.map_acl(case.raw, context),
            synced_at=fresh,
        )
    # Freshness negatives: listed, but beyond the window / stale-stamped.
    await _seed_doc(
        world,
        key=STALE_BEYOND_WINDOW,
        acl_enforced=True,
        principals=member_principal,
        synced_at=stale,
    )
    await _seed_doc(
        world, key=STALE_STAMPED, acl_enforced=True, principals=member_principal, synced_at=None
    )
    # A source-side revocation: the mirror no longer lists anyone (post-sync).
    await _seed_doc(
        world, key=REVOKED_AT_SOURCE, acl_enforced=True, principals=frozenset(), synced_at=fresh
    )
    # A plain upload — the non-enforced mode, unchanged — granted to the guest.
    guest_id = created[subject.guest_email]
    await _seed_doc(world, key=UPLOAD, acl_enforced=False, principals=frozenset(), synced_at=None)
    await _grant(world, UPLOAD, guest_id)
    # An enforced document with an empty mirror AND a Lumen grant — the grant
    # must not widen it (ADR-0019 §2 exclusive modes).
    await _seed_doc(
        world, key=GRANTED_ENFORCED, acl_enforced=True, principals=frozenset(), synced_at=fresh
    )
    await _grant(world, GRANTED_ENFORCED, guest_id)

    for doc in world.docs.values():
        await _index(world, doc.document_id)
    return world


async def _seed_doc(
    world: World,
    *,
    key: str,
    acl_enforced: bool,
    principals: frozenset[str],
    synced_at: datetime | None,
) -> None:
    async with db_session.session_scope() as session:
        documents = DocumentRepository(session, world.tenant_id)
        document = await documents.create(
            owner_id=world.owner_id,
            collection_id=world.collection_id,
            filename=f"{key}.txt",
            mime_type="text/plain",
            size_bytes=len(_TEXT),
            storage_key=f"{world.tenant_id}/{key}",
            acl_enforced=acl_enforced,
            status=DocumentStatus.READY,
            acl_principals=sorted(principals) if acl_enforced else None,
            acl_synced_at=synced_at,
        )
        [chunk] = await ChunkRepository(session, world.tenant_id).replace_for_document(
            document.id, [ChunkInput(text=f"{key} :: {_TEXT}", char_start=0, char_end=len(_TEXT))]
        )
        await session.commit()
    world.docs[key] = SeededDoc(
        key=key,
        document_id=document.id,
        chunk_id=chunk.id,
        acl_enforced=acl_enforced,
        principals=principals,
        fresh=synced_at is not None and synced_at >= acl_freshness_floor(),
    )


async def _grant(world: World, key: str, principal_id: uuid.UUID) -> None:
    doc = world.docs[key]
    async with db_session.session_scope() as session:
        session.add(
            models.Grant(
                tenant_id=world.tenant_id,
                resource_type="document",
                resource_id=doc.document_id,
                principal_type="user",
                principal_id=principal_id,
                role="viewer",
                granted_by=world.owner_id,
            )
        )
        await session.commit()
    world.docs[key] = SeededDoc(
        key=doc.key,
        document_id=doc.document_id,
        chunk_id=doc.chunk_id,
        acl_enforced=doc.acl_enforced,
        principals=doc.principals,
        fresh=doc.fresh,
        granted_to=doc.granted_to | {principal_id},
    )


async def _index(world: World, document_id: uuid.UUID) -> None:
    """Project + write one document through the REAL index-sync path."""
    from app.core.config import get_settings

    store = world.store()
    try:
        await sync_document_index_async(
            world.tenant_id, document_id, settings=get_settings(), store=store
        )
    finally:
        await store.aclose()


def raw_document(world: World, key: str) -> Mapping[str, object]:
    """The indexed engine document for a seeded key (for hygiene assertions)."""
    doc = world.docs[key]
    return next(d for d in world.engine.docs.values() if d["document_id"] == str(doc.document_id))


__all__ = [
    "GRANTED_ENFORCED",
    "REVOKED_AT_SOURCE",
    "STALE_BEYOND_WINDOW",
    "STALE_STAMPED",
    "UPLOAD",
    "SeededDoc",
    "World",
    "build_world",
    "raw_document",
]
