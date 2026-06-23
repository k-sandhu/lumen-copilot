"""audit query indexes — make ``audit_events`` filterable for GET /audit (#82)

The single M2 schema step (ADR-0008 §4, single-head invariant): it only adds what
the M2 *read* surfaces need. The Trust-Surfaces audit screen lists a tenant's
events newest-first and filters by **actor**, **event type** (``action``), and the
target **resource** — always within one tenant and ordered by ``ts``. The 0002
schema already covers the unfiltered list (``ix_audit_events_tenant_ts`` on
``(tenant_id, ts)``) and the standalone ``ix_audit_events_actor_id``; this revision
adds the three composite indexes that make each *filtered, time-ordered* slice
index-backed instead of a tenant-wide scan-then-sort:

* ``ix_audit_events_tenant_action_ts`` on ``(tenant_id, action, ts)`` — "show me
  the ``permission.denied`` (or any event-type) events, newest first".
* ``ix_audit_events_tenant_actor_ts`` on ``(tenant_id, actor_id, ts)`` — "show me
  what *this user* did, newest first" (the leading ``(tenant_id, actor_id)`` prefix
  also subsumes the per-tenant use of the old standalone ``actor_id`` index, but we
  leave that one in place — dropping it is out of this read-only-additive scope).
* ``ix_audit_events_tenant_resource_ts`` on ``(tenant_id, resource_id, ts)`` —
  "show me everything that touched *this resource*, newest first".

Each puts ``ts`` last so the same index serves both the equality filter and the
``ORDER BY ts DESC`` the read uses, with ``tenant_id`` leading so the INV-1 tenant
predicate is always the first column. Plain btree (the default) — no Postgres-only
operator class here, so the DDL also renders/applies on the SQLite test path.

**No new tables.** The risk-tier (T0–T3) map is static config, not a table (it is
a fixed policy lattice, never tenant-edited — spec 0004 §2.5), and the RBAC roles
(``member``/``admin``/``security``) already exist on ``users.roles`` from the auth
work (#19). The /admin and /audit reads need indexes, not reference tables.

Reversible: ``downgrade`` drops exactly the three indexes it created (the model in
``app.db.models`` declares the same three so autogenerate stays clean). Both legs
are plain ``CREATE/DROP INDEX`` inside the migration transaction.

Revision ID: 0005_audit_query_indexes
Revises: 0004_retrieval_indexes
Create Date: 2026-06-22 21:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_audit_query_indexes"
down_revision: str | None = "0004_retrieval_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Index names (stable, referenced by the downgrade and the model __table_args__).
_TENANT_ACTION_TS = "ix_audit_events_tenant_action_ts"
_TENANT_ACTOR_TS = "ix_audit_events_tenant_actor_ts"
_TENANT_RESOURCE_TS = "ix_audit_events_tenant_resource_ts"


def upgrade() -> None:
    # Filter by event type (action) within a tenant, newest-first.
    op.create_index(_TENANT_ACTION_TS, "audit_events", ["tenant_id", "action", "ts"])
    # Filter by actor within a tenant, newest-first.
    op.create_index(_TENANT_ACTOR_TS, "audit_events", ["tenant_id", "actor_id", "ts"])
    # Filter by target resource within a tenant, newest-first.
    op.create_index(_TENANT_RESOURCE_TS, "audit_events", ["tenant_id", "resource_id", "ts"])


def downgrade() -> None:
    op.drop_index(_TENANT_RESOURCE_TS, table_name="audit_events")
    op.drop_index(_TENANT_ACTOR_TS, table_name="audit_events")
    op.drop_index(_TENANT_ACTION_TS, table_name="audit_events")
