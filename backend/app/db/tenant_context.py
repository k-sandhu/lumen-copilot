"""Per-transaction tenant GUC — the RLS backstop's request/​task wiring (#17).

Postgres row-level security (the migration ``0007``) keys every tenant-scoped
table's policy off a **session GUC**, ``app.tenant_id``: a row is visible/writable
only when its ``tenant_id`` equals the value of that GUC for the current
transaction (spec 0004 §2.1, INV-1). This module is the single place that *sets*
that GUC, so the repository tenant predicate (the primary enforcement) gains a
database-level backstop: if a predicate is ever forgotten, RLS still returns zero
cross-tenant rows, and with **no** GUC set a tenant-scoped query returns nothing
(fails closed).

The GUC is bound **per transaction** (``set_config(..., is_local => true)`` — the
function form of ``SET LOCAL``), so it is scoped to exactly the unit of work that
set it and never leaks across pooled connections. Callers set it once at the
start of a transaction, right after the tenant is resolved:

* **request path** — the ``current_tenant`` dependency binds it on the request
  session (``app.api.deps``);
* **task path** — the ingestion task binds it on each ``session_scope`` it opens
  (``app.tasks.ingest``), keyed off the task's tenant argument;
* **chat answer runtime** — binds it on the runtime's own session
  (``app.services.chat_runtime``);
* **pre-identity/system paths** (login/refresh before the tenant is known, the
  seed) set the **bypass** sentinel (:func:`bind_bypass`) — a deliberate,
  audited exemption for the genuinely tenant-agnostic lookups (resolve *which*
  tenant an email/refresh-token belongs to). The policy admits the bypass
  sentinel explicitly; everything else is denied by default.

**Dialect-aware, offline-safe.** RLS and ``set_config`` are Postgres-only; the
unit/API tests run on in-memory SQLite (no RLS, no GUC). On any non-Postgres
dialect these helpers are a **no-op** — the SQLite tests already prove INV-1 via
the repository predicate, and the RLS backstop is asserted separately against a
real Postgres (the live negative tests, skipped offline).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The GUC the RLS policies read (matches the migration's policy expression).
TENANT_GUC: Final = "app.tenant_id"

# The sentinel that the policy treats as "tenant-agnostic, admit every row" — used
# ONLY by the deliberate pre-identity/system lookups (login resolving a tenant
# from an email, the seed). It is a fixed non-UUID string so it can never collide
# with a real ``tenant_id`` (a uuid), and the policy matches it textually before
# the uuid comparison, so a missing GUC still fails closed (no bypass by default).
BYPASS_SENTINEL: Final = "bypass"

_SET_GUC: Final = text(f"SELECT set_config('{TENANT_GUC}', :value, true)")


def _is_postgres(session: AsyncSession) -> bool:
    """True only when the session is bound to a Postgres engine.

    RLS / ``set_config`` exist only on Postgres; on the SQLite used by the
    offline tests these helpers must no-op (the bind may also be unset in some
    unit contexts — treat that as non-Postgres too).
    """
    bind = session.bind
    return bind is not None and bind.dialect.name == "postgresql"


async def bind_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """Bind ``app.tenant_id`` to ``tenant_id`` for the current transaction (RLS).

    After this, every tenant-scoped read/write on ``session`` sees only rows whose
    ``tenant_id`` equals ``tenant_id`` — the RLS backstop to the repository
    predicate (INV-1). Transaction-local (``SET LOCAL`` semantics), so it is
    confined to this unit of work and reset when the transaction ends. A no-op on
    a non-Postgres engine (offline SQLite tests).
    """
    if not _is_postgres(session):
        return
    await session.execute(_SET_GUC, {"value": str(tenant_id)})


async def bind_bypass(session: AsyncSession) -> None:
    """Bind the **bypass** sentinel for a deliberate tenant-agnostic transaction.

    The narrow, audited exemption for the pre-identity/system lookups that *must*
    run before a tenant is known: login resolving which tenant an email belongs
    to, the refresh path resolving a token's owner, and the dev seed creating the
    first tenant + user. These read/write across tenants by design; everything
    after identity resolution re-scopes to the resolved tenant. A no-op on a
    non-Postgres engine (offline SQLite tests).
    """
    if not _is_postgres(session):
        return
    await session.execute(_SET_GUC, {"value": BYPASS_SENTINEL})
