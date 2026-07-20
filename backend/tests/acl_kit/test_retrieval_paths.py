"""INV-2 across **every** retrieval path — for every ACL-declaring connector.

Spec 0004 §2.2's "there is no retrieval path that skips the filter", proven two
ways for a requester the mirror does not list:

* **executed** — ``document_search``, ``list_documents``, ``load_passages``,
  ``load_document_text``, ``get_permitted_document``, ``permitted_document_ids``
  and ``permitted_document_names`` are run for real against the seeded corpus;
* **structural** — ``semantic_search`` and ``lexical_search`` emit pgvector /
  ``tsvector`` SQL that no offline dialect can execute, so those two are proven
  by compiling the statement they build and asserting the mode-split predicate
  is in its ``WHERE`` clause. That check is then generalised: *every* builder in
  ``retrieval/queries`` that reads documents or chunks must carry it, so a new
  unfiltered path fails here the day it is added.

Direct fetch is the INV-2 "→ 404" half: the point read returns ``None``, which
is what the API maps to 404 (existence non-disclosure, never 403).
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy.dialects import postgresql

import app.db.session as db_session
from app.retrieval import queries

from .subject import AclSubject
from .subjects import SUBJECT_IDS, SUBJECTS
from .world import GRANTED_ENFORCED, REVOKED_AT_SOURCE, STALE_STAMPED, World

pytestmark = pytest.mark.parametrize("subject", SUBJECTS, ids=SUBJECT_IDS)

# Documents the "outsider" (a real tenant member the mirror never lists) must not
# reach through ANY path.
_DENIED_KEYS = ("case:direct_user", "case:empty_acl", REVOKED_AT_SOURCE, STALE_STAMPED)


def _outsider(world: World, subject: AclSubject) -> uuid.UUID:
    """A same-tenant member with no mirror entry, no ownership and no grant."""
    return world.user_id(subject.unattested_email)


# --- executed paths -----------------------------------------------------------


async def test_non_acl_member_is_excluded_from_every_executed_path(
    world: World, subject: AclSubject
) -> None:
    """The outsider retrieves nothing they are not in the mirror for."""
    user_id = _outsider(world, subject)
    allow = world.allow_set(user_id)
    denied = [world.docs[key] for key in _DENIED_KEYS]

    async with db_session.session_scope() as session:
        listed = await queries.list_documents(session, allow_set=allow, k=200)
        searched = await queries.document_search(session, allow_set=allow, name_or_query="", k=200)
        passages = await queries.load_passages(
            session, allow_set=allow, chunk_ids=[d.chunk_id for d in denied]
        )
        permitted_ids = await queries.permitted_document_ids(
            session, allow_set=allow, document_ids=[d.document_id for d in denied]
        )
        names = (
            await session.execute(
                queries.permitted_document_names(
                    allow_set=allow, document_ids=[d.document_id for d in denied]
                )
            )
        ).all()

    visible_ids = {row.document_id for row in listed} | {row.document_id for row in searched}
    for doc in denied:
        assert doc.document_id not in visible_ids, f"{doc.key} leaked into a listing path"
        assert doc.chunk_id not in passages, f"{doc.key} leaked into hydration"
        assert doc.document_id not in permitted_ids, f"{doc.key} leaked into the id filter"
    assert names == []


async def test_direct_fetch_of_a_denied_document_is_not_found(
    world: World, subject: AclSubject
) -> None:
    """INV-2's second half: direct fetch → ``None`` → the API's 404."""
    user_id = _outsider(world, subject)
    allow = world.allow_set(user_id)
    async with db_session.session_scope() as session:
        for key in _DENIED_KEYS:
            document_id = world.docs[key].document_id
            assert (
                await queries.get_permitted_document(
                    session, allow_set=allow, document_id=document_id
                )
                is None
            ), f"{key} was fetchable directly"
            assert (
                await queries.load_document_text(session, allow_set=allow, document_id=document_id)
                is None
            ), f"{key} text was readable directly"


async def test_the_owner_cannot_point_read_an_enforced_document(
    world: World, subject: AclSubject
) -> None:
    """Connector documents are *owned* by the connecting admin — the point read
    must still refuse them (the ``/documents/{id}`` escalation, ADR-0019 §2)."""
    allow = world.allow_set(world.owner_id)
    async with db_session.session_scope() as session:
        for key in ("case:empty_acl", REVOKED_AT_SOURCE, STALE_STAMPED, GRANTED_ENFORCED):
            got = await queries.get_permitted_document(
                session, allow_set=allow, document_id=world.docs[key].document_id
            )
            assert got is None, f"the owner point-read {key}"


async def test_hydration_recheck_drops_denied_chunks(world: World, subject: AclSubject) -> None:
    """Defense in depth: engine hits for denied documents never become passages.

    Feeds ``load_passages`` every chunk in the corpus — the shape a compromised
    or stale engine result would have — and asserts only the permitted ones
    hydrate.
    """
    listed_user = world.user_id(subject.attested_email)
    allow = world.allow_set(listed_user)
    async with db_session.session_scope() as session:
        rows = await queries.load_passages(
            session, allow_set=allow, chunk_ids=[d.chunk_id for d in world.docs.values()]
        )
    hydrated = {world.key_of(row.document_id) for row in rows.values()}
    assert hydrated == world.expected_visible(listed_user)


async def test_engine_candidates_for_a_denied_document_die_at_hydration(
    world: World, subject: AclSubject
) -> None:
    """A stale engine can surface a denied candidate; Postgres is the backstop.

    Simulates exactly the ADR-0019 §3 window where the index has not yet learned
    a stale-stamp: the chunk id is handed to hydration as if the engine returned
    it, and the re-check drops it.
    """
    outsider = _outsider(world, subject)
    stamped = world.docs[STALE_STAMPED]
    async with db_session.session_scope() as session:
        rows = await queries.load_passages(
            session, allow_set=world.allow_set(outsider), chunk_ids=[stamped.chunk_id]
        )
    assert rows == {}


# --- structural: no builder may skip the predicate ----------------------------

# Builders that deliberately do not carry the document predicate, with the reason
# they are safe. Anything else in `retrieval/queries` must filter.
_EXEMPT: dict[str, str] = {
    "valid_chunk_pairs": "tenant-scoped chunk-existence check; its caller joins it "
    "to an already-permitted document set (#446 defense in depth)",
}

_ACL_MARKERS = ("acl_enforced", "acl_principals", "acl_synced_at")


def _compiled_sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        )
    )


async def test_semantic_and_lexical_paths_carry_the_mode_split_predicate(
    world: World, subject: AclSubject
) -> None:
    """The two Postgres-only paths, proven by the SQL they emit.

    ``pgvector``'s ``<=>`` and ``to_tsvector`` cannot run on the offline
    database, so these paths are proven structurally: the statement they build
    is compiled to PostgreSQL and must contain the enforced branch (the
    ``acl_enforced`` term, the principal overlap and the freshness floor) plus
    the tenant predicate.
    """
    allow = world.allow_set(world.user_id(subject.attested_email))
    for builder in (queries.semantic_search, queries.lexical_search):
        recorder = _StatementRecorder()
        kwargs: dict[str, object] = {"allow_set": allow, "k": 5}
        if builder is queries.semantic_search:
            kwargs["query_embedding"] = [0.1] * 8
        else:
            kwargs["query"] = "fox"
        with pytest.raises(_Stop):
            await builder(recorder, **kwargs)  # type: ignore[arg-type]
        sql = recorder.captured[0]
        assert "tenant_id" in sql, builder.__name__
        for marker in _ACL_MARKERS:
            assert marker in sql, f"{builder.__name__} lost {marker}"


class _Stop(Exception):
    """Aborts a query builder once its statement has been captured."""


class _StatementRecorder:
    """A session stand-in that compiles the statement and stops the query."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    async def execute(self, statement: object) -> object:
        self.captured.append(_compiled_sql(statement))
        raise _Stop()


def test_no_retrieval_builder_reads_documents_without_the_predicate(
    world: World, subject: AclSubject
) -> None:
    """The chokepoint is structural: every public builder takes an allow-set.

    A new retrieval path that forgot the filter would not take an ``allow_set``
    at all — this pins the whole module's surface so that regression is caught
    at the signature, before anyone has to notice a missing ``WHERE``.
    """
    inspected: list[str] = []
    unfiltered: list[str] = []
    for name, member in vars(queries).items():
        if name.startswith("_") or not callable(member):
            continue
        if getattr(member, "__module__", None) != queries.__name__:
            continue
        if name in _EXEMPT or not inspect.isfunction(member):
            continue
        inspected.append(name)
        if "allow_set" not in inspect.signature(member).parameters:
            unfiltered.append(name)
    assert not unfiltered, (
        f"retrieval builders without an allow-set: {sorted(unfiltered)} — "
        "every path through retrieval/ must carry the INV-2 predicate "
        f"(exempt, with reasons: {sorted(_EXEMPT)})"
    )
    # Non-vacuity: the scan really did reach the paths the ADR names, so a
    # future refactor that hides them from `vars()` fails instead of passing.
    assert {
        "semantic_search",
        "lexical_search",
        "document_search",
        "list_documents",
        "load_document_text",
        "load_passages",
        "get_permitted_document",
    } <= set(inspected), f"the builder scan missed known paths (saw {sorted(inspected)})"


def test_the_exempt_list_stays_justified(world: World, subject: AclSubject) -> None:
    """Every exemption names a live function and carries a reason."""
    for name, reason in _EXEMPT.items():
        assert hasattr(queries, name), f"stale exemption {name!r}"
        assert reason, f"exemption {name!r} must say why it is safe"
