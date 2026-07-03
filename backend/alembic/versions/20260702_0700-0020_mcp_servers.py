"""per-tenant MCP server registration — mcp_servers (#226, ADR-0012 §5)

The persistence half of MCP server registration (issue #226, ADR-0012 §5): a
tenant/owner-scoped ``mcp_servers`` row is one registered remote MCP server —
its display name, remote ``transport`` (streamable_http|sse — stdio can never be
stored, §1), https ``endpoint_url`` (SSRF-checked in the service before a write
and again on every connect), an ``auth_secret_ref`` → the CC-C secrets vault
(#209; the credential itself is **never** in this table), a masked ``secret_hint``
for the UI, the ``enabled`` toggle, the health ``status`` machine, the last
``last_health_at`` / ``last_error``, and the ``discovered_tools`` jsonb snapshot
from the last successful probe. ``down_revision`` is the current head
``0019_code_runs`` (single-head invariant, ADR-0008 §4).

One new tenant/owner-scoped table:

* ``mcp_servers`` — the registration record. Non-null ``tenant_id`` (INV-1) +
  ``owner_id`` (the registering user, INV-2; CASCADE — a deprovisioned user's
  servers are removed with them); nullable ``auth_secret_ref`` FK → ``secrets``
  (**SET NULL** so deleting the vault secret does not cascade-delete the server);
  a nullable masked ``secret_hint`` (never the value); ``enabled`` (default true);
  a CHECK-pinned ``transport`` (streamable_http|sse) and ``status``
  (pending|ready|error) enum domain; nullable ``last_health_at`` / ``last_error``;
  ``discovered_tools`` jsonb (default empty); ``created_at`` / ``updated_at``. A
  tenant-leading ``(tenant_id, owner_id)`` index backs the owner list/lookup.

RLS: ``mcp_servers`` is tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security with the same fail-closed ``app.tenant_id`` GUC policy the
``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1) —
additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS,
drops the indexes, and drops the table, restoring the pre-#226 state. Offline DDL
render asserts the shape; the live apply runs against a disposable throwaway
database (the #70 lesson).

Revision ID: 0020_mcp_servers
Revises: 0019_code_runs
Create Date: 2026-07-02 07:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0020_mcp_servers"
down_revision: str | None = "0019_code_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The GUC + bypass sentinel the RLS policy reads — kept in lockstep with the
# ``0007`` backstop and ``app.db.tenant_context`` (TENANT_GUC / BYPASS_SENTINEL).
_GUC = "app.tenant_id"
_BYPASS = "bypass"
_PREDICATE = (
    f"current_setting('{_GUC}', true) = '{_BYPASS}' "
    f"OR tenant_id = current_setting('{_GUC}', true)::uuid"
)

# JSON-typed columns: JSONB on Postgres (indexable), generic JSON on SQLite (the
# offline test engine). Mirrors ``app.db.models._JSON``.
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")  # type: ignore[no-untyped-call]

# The tenant-scoped table that gets the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("mcp_servers",)


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The registering user (INV-2). CASCADE — a deprovisioned user's servers go
        # with them (unlike a completed run record, a registration is live config).
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        # → a CC-C secret (#209); NEVER the credential itself. SET NULL so deleting
        # the vault secret does not cascade-delete the server row.
        sa.Column(
            "auth_secret_ref",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("secrets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # A masked tail of the stored credential for the UI (e.g. ``****abcd``);
        # never the value. NULL for an anonymous server (no auth stored).
        sa.Column("secret_hint", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        # The last list_tools() snapshot (names + schemas + read-only annotations).
        sa.Column("discovered_tools", _JSON, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Pin the transport + status enum domains at the DB so a bad value (incl. a
        # deferred ``stdio`` transport, ADR-0012 §1) can never be stored.
        sa.CheckConstraint(
            "transport in ('streamable_http', 'sse')",
            name="ck_mcp_servers_transport",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'ready', 'error')",
            name="ck_mcp_servers_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_mcp_servers_tenant_id", "mcp_servers", ["tenant_id"])
    op.create_index("ix_mcp_servers_owner_id", "mcp_servers", ["owner_id"])
    # The "this owner's servers" list/lookup path.
    op.create_index("ix_mcp_servers_tenant_owner", "mcp_servers", ["tenant_id", "owner_id"])

    # --- RLS backstop, consistent with the 0007 per-table policy ------------
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_{table} ON {table} "
                f"USING ({_PREDICATE}) "
                f"WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    # Reverse order. Drop RLS first, then the indexes, then the table.
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_mcp_servers_tenant_owner", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_owner_id", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_tenant_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
