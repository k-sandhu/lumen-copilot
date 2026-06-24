"""tenancy RLS backstop — ENABLE+FORCE row-level security per scoped table (#17)

The defense-in-depth half of CC-2 (spec 0004 §2.1): the repository ``tenant_id``
predicate is the *primary* tenant isolation (INV-1); this migration adds the
**Postgres RLS backstop** so a forgotten predicate still cannot leak across
tenants. ``down_revision`` is the current head ``0006_sources`` (single-head
invariant, ADR-0008 §4).

For every tenant-scoped table — ``users``, ``refresh_tokens``, ``collections``,
``sources``, ``documents``, ``chunks``, ``chat_sessions``, ``messages``,
``citations``, ``audit_events`` — it:

* ``ENABLE ROW LEVEL SECURITY`` (turn policies on), and
* ``FORCE ROW LEVEL SECURITY`` so the policy applies **even to the table owner**
  (the app role is the owner; without FORCE the owner bypasses RLS and the
  backstop would be inert for the very role the app runs as), and
* adds one policy ``rls_<table>`` keyed off the per-transaction GUC
  ``app.tenant_id`` (set by ``app.db.tenant_context``):

      USING      (current_setting('app.tenant_id', true) = 'bypass'
                  OR tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (same)

  ``current_setting(..., true)`` returns NULL when the GUC is unset → the uuid
  comparison is NULL → no rows (and a write is rejected): **fail-closed**. The
  ``= 'bypass'`` leg is the deliberate exemption the pre-identity/system paths
  (login resolving a tenant from an email, the seed) opt into explicitly; it can
  never match a real ``tenant_id`` (a uuid). ``USING`` gates reads/updates/deletes;
  ``WITH CHECK`` gates the row a write produces, so a write can neither read nor
  create another tenant's row.

The parent ``tenants`` table is **not** scoped (it has no ``tenant_id``); it is
left alone so provisioning/resolution still works. ``audit_events`` keeps its
append-only REVOKE (0002) — RLS is orthogonal and additive.

Reversible (backend/AGENTS.md): ``downgrade`` drops every policy and disables
RLS, restoring the pre-#17 state. Migrations themselves run DDL (not row DML),
so enabling RLS here does not require a GUC; the live round-trip test inserts no
rows. Offline DDL render asserts the statements; the live apply runs against a
disposable throwaway database (the #70 lesson).

Revision ID: 0007_tenancy_rls
Revises: 0006_sources
Create Date: 2026-06-24 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_tenancy_rls"
down_revision: str | None = "0006_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every tenant-scoped table (carries a non-null ``tenant_id`` FK → ``tenants``).
# ``tenants`` is the isolation root and is intentionally excluded. Kept in
# dependency order for readability; RLS application order is irrelevant.
_SCOPED_TABLES: tuple[str, ...] = (
    "users",
    "refresh_tokens",
    "collections",
    "sources",
    "documents",
    "chunks",
    "chat_sessions",
    "messages",
    "citations",
    "audit_events",
)

# The GUC the policy reads + the bypass sentinel — kept in lockstep with
# ``app.db.tenant_context`` (TENANT_GUC / BYPASS_SENTINEL).
_GUC = "app.tenant_id"
_BYPASS = "bypass"

# Fail-closed predicate: NULL GUC (unset) → uuid comparison NULL → no rows; the
# explicit bypass sentinel is the only tenant-agnostic exemption.
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)


def upgrade() -> None:
    for table in _SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the policy binds the table owner too (the app role owns the
        # tables); without it the owner silently bypasses RLS.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_{table} ON {table} "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    # Reverse: drop the policy, then disable RLS — restoring the pre-#17 state.
    for table in _SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
