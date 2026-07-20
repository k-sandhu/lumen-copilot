"""INV-2 across **every** retrieval path — for every ACL-declaring connector.

Spec 0004 §2.2's "there is no retrieval path that skips the filter", proven two
ways for a requester the mirror does not list:

* **executed** — ``document_search``, ``list_documents``, ``load_passages``,
  ``load_document_text``, ``get_permitted_document``, ``permitted_document_ids``
  and ``permitted_document_names`` are run for real against the seeded corpus;
* **structural** — ``semantic_search`` and ``lexical_search`` emit pgvector /
  ``tsvector`` SQL that no offline dialect can execute, so those two are proven
  by **whole-expression** comparison: the WHERE clause is decomposed into its
  top-level conjuncts and one must compile byte-identically to
  ``_document_permitted(allow)``. The same check runs over *every* public builder
  — including the executable ones — so the module gate proves predicate **use**,
  not parameter presence, and a new unfiltered path fails the day it is added.

Direct fetch is the INV-2 "→ 404" half: the point read returns ``None``, which
is what the API maps to 404 (existence non-disclosure, never 403).
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import and_, or_
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import CompileError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BooleanClauseList

import app.db.session as db_session
from app.db import models
from app.retrieval import queries
from app.retrieval.permissions import AllowSet

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


# --- structural: every builder must APPLY the predicate, not merely accept it --
#
# ``semantic_search`` and ``lexical_search`` emit pgvector ``<=>`` / ``to_tsvector``
# SQL that no offline dialect can execute, so they cannot be proven by running
# them. They are proven instead by **whole-expression** comparison: the WHERE
# clause each builder emits is decomposed into its top-level conjuncts, and one
# of those conjuncts must compile byte-identically to ``_document_permitted(allow)``
# — the chokepoint predicate itself, rendered with the same allow-set.
#
# Why that is not circular: ``_document_permitted``'s *semantics* are pinned
# exhaustively by the executed proofs in ``test_stores`` and above in this module,
# against real rows in both stores. What remains unprovable offline for these two
# builders is only whether they apply THE predicate, unmodified — and that is
# exactly what an expression-equality check establishes. A widened predicate such
# as ``tenant AND (mode_split OR owner_id IS NOT NULL)`` yields a conjunct that is
# not the chokepoint expression and fails; hoisting an ``OR`` to the top level
# collapses the WHERE into a single non-matching conjunct and fails too.
#
# The same check runs over EVERY public builder — including the executable ones —
# so the module gate proves predicate *use*, not parameter presence: a builder
# that accepts ``allow_set`` and ignores it has no matching conjunct.

# Builders that deliberately do not carry the document predicate, with the reason
# they are safe. Anything else in `retrieval/queries` must filter.
_EXEMPT: dict[str, str] = {
    "valid_chunk_pairs": "tenant-scoped chunk-existence check; its caller joins it "
    "to an already-permitted document set (#446 defense in depth)",
}

# How to drive each builder far enough to capture its statement. Every public
# builder must appear here (or in _EXEMPT) — see the completeness gate below.
_ASYNC_BUILDERS: dict[str, Callable[[object, AllowSet], Awaitable[object]]] = {
    "semantic_search": lambda s, a: queries.semantic_search(
        s, allow_set=a, query_embedding=[0.1] * 8, k=5
    ),
    "lexical_search": lambda s, a: queries.lexical_search(s, allow_set=a, query="fox", k=5),
    "document_search": lambda s, a: queries.document_search(s, allow_set=a, name_or_query="f", k=5),
    "list_documents": lambda s, a: queries.list_documents(s, allow_set=a, k=5),
    "load_passages": lambda s, a: queries.load_passages(s, allow_set=a, chunk_ids=[_PROBE_ID]),
    "load_document_text": lambda s, a: queries.load_document_text(
        s, allow_set=a, document_id=_PROBE_ID
    ),
    "get_permitted_document": lambda s, a: queries.get_permitted_document(
        s, allow_set=a, document_id=_PROBE_ID
    ),
    "permitted_document_ids": lambda s, a: queries.permitted_document_ids(
        s, allow_set=a, document_ids=[_PROBE_ID]
    ),
}

# Builders that return a statement directly (no session, nothing to execute).
_STATEMENT_BUILDERS: dict[str, Callable[[AllowSet], object]] = {
    "permitted_document_names": lambda a: queries.permitted_document_names(
        allow_set=a, document_ids=[_PROBE_ID]
    ),
}

_PROBE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000ff")

# The freshness floor is relative to *now*, so two renderings taken microseconds
# apart differ in their literal. Pinning it makes the comparison purely about the
# predicate's structure; the floor's VALUE is proven separately (test_stores'
# shared-window check) and is not this module's business.
_FROZEN_FLOOR = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_freshness_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "acl_freshness_floor", lambda *a, **k: _FROZEN_FLOOR)


def _matches(expected: str, conjuncts: list[str]) -> bool:
    """Is ``expected`` one of the conjuncts, modulo SQLAlchemy's grouping parens?

    A composite operand nested inside an ``AND`` is rendered wrapped; the same
    expression compiled standalone is not. That is formatting, not semantics.
    """
    return expected in conjuncts or f"({expected})" in conjuncts


class _Stop(Exception):
    """Aborts a query builder once its statement has been captured."""


class _StatementRecorder:
    """A session stand-in that captures the statement and stops the query."""

    def __init__(self) -> None:
        self.captured: list[object] = []

    async def execute(self, statement: object) -> object:
        self.captured.append(statement)
        raise _Stop()


def _rendered(clause: object) -> str:
    """One expression, compiled to PostgreSQL with its bind values inlined.

    ``literal_binds`` makes the comparison concrete: two expressions match only
    when they are the same predicate over the same allow-set, not merely the
    same shape with different parameters.
    """
    try:
        return str(
            clause.compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
    except CompileError:
        # A few vendor literals have no literal renderer (``lexical_search``'s
        # ``to_tsvector('english', ...)`` REGCONFIG among them). Those conjuncts
        # are never the permission or tenant predicate — which do render — so
        # degrading them to parameterised form leaves the comparison intact.
        return str(clause.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def _conjuncts(clause: object) -> list[object]:
    """Flatten a WHERE clause into its top-level ``AND`` operands.

    Anything that is not an ``AND`` list is a single conjunct — which is what
    makes a hoisted top-level ``OR`` fail: the whole clause becomes one operand
    that cannot equal the chokepoint predicate.
    """
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        flattened: list[object] = []
        for operand in clause.clauses:
            flattened.extend(_conjuncts(operand))
        return flattened
    return [clause]


async def _captured_where(name: str, allow: AllowSet) -> list[str]:
    """The rendered top-level conjuncts of one builder's WHERE clause."""
    if name in _STATEMENT_BUILDERS:
        statement = _STATEMENT_BUILDERS[name](allow)
    else:
        recorder = _StatementRecorder()
        with pytest.raises(_Stop):
            await _ASYNC_BUILDERS[name](recorder, allow)
        statement = recorder.captured[0]
    where = statement.whereclause  # type: ignore[attr-defined]
    assert where is not None, f"{name} emits no WHERE clause at all"
    return [_rendered(operand) for operand in _conjuncts(where)]


@pytest.mark.parametrize("builder", sorted({*_ASYNC_BUILDERS, *_STATEMENT_BUILDERS}))
async def test_every_builder_applies_the_chokepoint_predicate_verbatim(
    world: World, subject: AclSubject, builder: str
) -> None:
    """Predicate USE, not parameter presence — for every retrieval path.

    The exclusive mode split must appear as a whole conjunct, identical to
    ``_document_permitted(allow)``. Accepting an ``allow_set`` and ignoring it,
    widening the predicate, or hoisting an ``OR`` above it all fail here.
    """
    allow = world.allow_set(world.user_id(subject.attested_email))
    conjuncts = await _captured_where(builder, allow)
    expected = _rendered(queries._document_permitted(allow))
    assert _matches(expected, conjuncts), (
        f"{builder} does not apply the mode-split predicate verbatim.\n"
        f"expected conjunct: {expected}\ngot conjuncts: {conjuncts}"
    )


@pytest.mark.parametrize("builder", sorted({*_ASYNC_BUILDERS, *_STATEMENT_BUILDERS}))
async def test_every_builder_binds_the_tenant_predicate(
    world: World, subject: AclSubject, builder: str
) -> None:
    """INV-1 sits outside the mode split and is never optional."""
    allow = world.allow_set(world.user_id(subject.attested_email))
    conjuncts = await _captured_where(builder, allow)
    tenant_terms = (
        _rendered(models.Document.tenant_id == allow.tenant_id),
        _rendered(models.Chunk.tenant_id == allow.tenant_id),
    )
    assert any(
        _matches(term, conjuncts) for term in tenant_terms
    ), f"{builder} does not bind the tenant predicate.\ngot conjuncts: {conjuncts}"


async def test_a_widened_predicate_is_rejected(world: World, subject: AclSubject) -> None:
    """Meta-proof: the expression check above really does catch a widening.

    Rebuilds the reviewer's counterexample — ``mode_split OR owner_id IS NOT
    NULL`` — and asserts it does NOT match the chokepoint expression. Without
    this, "the conjunct is present" could be true of any predicate containing
    the right column names.
    """
    allow = world.allow_set(world.user_id(subject.attested_email))
    expected = _rendered(queries._document_permitted(allow))
    widened = _rendered(
        or_(queries._document_permitted(allow), models.Document.owner_id.is_not(None))
    )
    assert widened != expected
    # ...and a stricter-looking but different predicate is rejected as well.
    narrowed = _rendered(
        and_(queries._document_permitted(allow), models.Document.owner_id.is_not(None))
    )
    assert narrowed != expected


def test_every_public_builder_is_covered_by_the_structural_gate(
    world: World, subject: AclSubject
) -> None:
    """A new retrieval path cannot be added without proving it filters.

    Anything public in ``retrieval/queries`` must be driven by the gate above or
    carry a written exemption — so "I added a query and forgot the predicate"
    fails at the completeness check even before the expression check runs.
    """
    covered = {*_ASYNC_BUILDERS, *_STATEMENT_BUILDERS, *_EXEMPT}
    public: set[str] = set()
    for name, member in vars(queries).items():
        if name.startswith("_") or not inspect.isfunction(member):
            continue
        if getattr(member, "__module__", None) != queries.__name__:
            continue
        public.add(name)
    assert public <= covered, (
        f"retrieval builders with no structural proof: {sorted(public - covered)} — "
        "add them to _ASYNC_BUILDERS/_STATEMENT_BUILDERS, or to _EXEMPT with a reason"
    )
    # Non-vacuity: the scan really reached the paths the ADR names.
    assert {"semantic_search", "lexical_search", "list_documents"} <= public
    # And nothing is listed that no longer exists.
    assert covered <= public | {"valid_chunk_pairs"}


def test_the_exempt_list_stays_justified(world: World, subject: AclSubject) -> None:
    """Every exemption names a live function and carries a reason."""
    for name, reason in _EXEMPT.items():
        assert hasattr(queries, name), f"stale exemption {name!r}"
        assert reason, f"exemption {name!r} must say why it is safe"
