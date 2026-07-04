"""per-tenant LLM providers — llm_providers (foundation: registration + model discovery)

The persistence half of per-tenant LLM provider registration (foundation PR): a
tenant/owner-scoped ``llm_providers`` row is one registered OpenAI-compatible
provider — its display name, ``provider_type`` (``openai_compatible`` for now —
OpenRouter/OpenAI/vLLM all speak the OpenAI ``/v1/models`` + ``/v1/chat/completions``
shape; the enum is small but extensible), ``base_url`` (the API root, SSRF-checked
in the service before a write and again on every discovery), an
``api_key_secret_ref`` → the CC-C secrets vault (#209; the key itself is **never**
in this table), a masked ``secret_hint`` for the UI, the ``enabled`` toggle, the
discovery ``status`` machine, the last ``last_discovery_at`` / ``last_error``, and
the ``discovered_models`` jsonb snapshot from the last successful ``GET /models``.
Mirrors the ``0020_mcp_servers`` migration throughout. ``down_revision`` is the
current head ``0022_sandbox_policy`` (single-head invariant, ADR-0008 §4).

This is the model-catalog foundation only: routing chat/embeddings through a
registered provider (and surfacing it in the chat picker) is a follow-up PR.

One new tenant/owner-scoped table:

* ``llm_providers`` — the registration record. Non-null ``tenant_id`` (INV-1) +
  ``owner_id`` (the registering admin, INV-2; CASCADE — a deprovisioned admin's
  providers are removed with them); nullable ``api_key_secret_ref`` FK → ``secrets``
  (**SET NULL** so deleting the vault secret does not cascade-delete the provider);
  a nullable masked ``secret_hint`` (never the value); ``enabled`` (default true); a
  CHECK-pinned ``provider_type`` (openai_compatible) and ``status``
  (pending|ready|error) enum domain; nullable ``last_discovery_at`` / ``last_error``;
  ``discovered_models`` jsonb (default empty); ``created_at`` / ``updated_at``. A
  tenant-leading ``(tenant_id, owner_id)`` index backs the owner list/lookup and a
  ``(tenant_id, enabled)`` index the enabled-provider read.

RLS: ``llm_providers`` is tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security with the same fail-closed ``app.tenant_id`` GUC policy the
``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1) —
additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, drops
the indexes, and drops the table, restoring the pre-foundation state. Offline DDL
render asserts the shape; the live apply runs against a disposable throwaway
database (the #70 lesson).

Revision ID: 0028_llm_providers
Revises: 0027_tenant_logo
Create Date: 2026-07-02 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0028_llm_providers"
down_revision: str | None = "0027_tenant_logo"
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
_SCOPED_TABLES: tuple[str, ...] = ("llm_providers",)


def upgrade() -> None:
    op.create_table(
        "llm_providers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The registering admin (INV-2). CASCADE — a deprovisioned admin's providers
        # go with them (live config, not a completed record).
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider_type", sa.String(length=30), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        # → a CC-C secret (#209); NEVER the API key itself. SET NULL so deleting the
        # vault secret does not cascade-delete the provider row.
        sa.Column(
            "api_key_secret_ref",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("secrets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # A masked tail of the stored API key for the UI (e.g. ``****abcd``); never
        # the value. NULL for an anonymous provider (no key stored).
        sa.Column("secret_hint", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        # The last GET /models snapshot: a list of {"id": str, "label": str | null}.
        sa.Column("discovered_models", _JSON, nullable=False, server_default="[]"),
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
        # Pin the provider_type + status enum domains at the DB so a bad value can
        # never be stored (the enum is small but extensible via a later migration).
        sa.CheckConstraint(
            "provider_type in ('openai_compatible')",
            name="ck_llm_providers_provider_type",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'ready', 'error')",
            name="ck_llm_providers_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_llm_providers_tenant_id", "llm_providers", ["tenant_id"])
    op.create_index("ix_llm_providers_owner_id", "llm_providers", ["owner_id"])
    # The "this owner's providers" list/lookup path + the enabled-provider read.
    op.create_index("ix_llm_providers_tenant_owner", "llm_providers", ["tenant_id", "owner_id"])
    op.create_index("ix_llm_providers_tenant_enabled", "llm_providers", ["tenant_id", "enabled"])

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

    op.drop_index("ix_llm_providers_tenant_enabled", table_name="llm_providers")
    op.drop_index("ix_llm_providers_tenant_owner", table_name="llm_providers")
    op.drop_index("ix_llm_providers_owner_id", table_name="llm_providers")
    op.drop_index("ix_llm_providers_tenant_id", table_name="llm_providers")
    op.drop_table("llm_providers")
