"""per-tenant sandbox governance policy — tenant_sandbox_policy (#233)

The persistence half of admin sandbox governance (issue #233): a tenant-scoped
``tenant_sandbox_policy`` row is an admin's per-tenant policy for the code-execution
sandbox (#230/#231) — whether code execution is ``enabled`` for the tenant, the
package allow/deny lists, the outbound egress posture (``egress_allowed`` +
``egress_allowlist``), and the runtime / memory / quota caps. **Deny-by-default,
fail-closed: the absence of a row means code execution stays DISABLED for the
tenant** (matching #230's deploy-wide kill-switch default OFF). The stored caps are
CEILINGS the enforcement path clamps to the deploy-wide ``SANDBOX_*`` config — a
per-tenant policy can only NARROW, never widen (the metadata IP is never
egress-allowlistable; a runtime above the config cap is clamped). ``down_revision``
is the current head ``0021_tenant_tool_policy`` (single-head invariant, ADR-0008 §4).

One new tenant-scoped table:

* ``tenant_sandbox_policy`` — the per-tenant policy record. Non-null ``tenant_id``
  (INV-1); ``enabled`` boolean (deny-by-default); ``allowed_packages`` /
  ``denied_packages`` / ``egress_allowlist`` JSON string arrays; ``egress_allowed``
  boolean; ``max_runtime_s`` / ``max_memory_mb`` / ``daily_runtime_cap_s`` /
  ``max_concurrency`` integer caps; ``updated_by`` FK → ``users`` (**SET NULL** so a
  deprovisioned admin does not cascade-delete the tenant's live policy);
  ``created_at`` / ``updated_at``. A unique ``(tenant_id)`` constraint makes the
  policy a per-tenant singleton (one row per tenant); the constraint's implicit index
  also serves the tenant-leading lookup.

RLS: ``tenant_sandbox_policy`` is tenant-scoped, so this migration ``ENABLE`` +
``FORCE`` row-level security with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1) —
additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#233 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (the #70
lesson).

Revision ID: 0022_sandbox_policy
Revises: 0021_tenant_tool_policy
Create Date: 2026-07-02 09:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_sandbox_policy"
down_revision: str | None = "0021_tenant_tool_policy"
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

# The tenant-scoped table that gets the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("tenant_sandbox_policy",)

# JSON type: portable across Postgres (the deploy) + SQLite (offline tests), so the
# ORM model and this migration agree without a dialect branch.
_JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tenant_sandbox_policy",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Deny-by-default: a row's presence does NOT imply enabled — an admin must
        # explicitly turn it on (and the deploy-wide kill-switch must also be on).
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # Package governance: an allowlist (empty ⇒ curated base image only) + a
        # denylist (takes precedence). JSON string arrays.
        sa.Column("allowed_packages", _JSON, nullable=False),
        sa.Column("denied_packages", _JSON, nullable=False),
        # Egress governance: deny-by-default network posture + the host:port allowlist
        # (the metadata IP is stripped in the service — never reachable, G4).
        sa.Column("egress_allowed", sa.Boolean(), nullable=False),
        sa.Column("egress_allowlist", _JSON, nullable=False),
        # Runtime / memory / quota caps — CEILINGS the enforcement path clamps to the
        # deploy-wide SANDBOX_* config (a per-tenant value only ever narrows).
        sa.Column("max_runtime_s", sa.Integer(), nullable=False),
        sa.Column("max_memory_mb", sa.Integer(), nullable=False),
        sa.Column("daily_runtime_cap_s", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        # The admin who last set this policy (INV-6 corroboration). SET NULL so a
        # deprovisioned admin does not cascade-delete the tenant's live policy.
        sa.Column(
            "updated_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        # One policy per tenant (a per-tenant singleton / upsert). The implicit index
        # also serves the tenant-leading policy lookup (INV-1).
        sa.UniqueConstraint("tenant_id", name="uq_tenant_sandbox_policy_tenant"),
        # Positive caps at the DB — a non-positive cap would disable an isolation
        # control (mirrors the config fail-fast rule, ADR-0013 §2/§6).
        sa.CheckConstraint("max_runtime_s > 0", name="ck_tenant_sandbox_policy_runtime_pos"),
        sa.CheckConstraint("max_memory_mb > 0", name="ck_tenant_sandbox_policy_memory_pos"),
        sa.CheckConstraint(
            "daily_runtime_cap_s > 0", name="ck_tenant_sandbox_policy_daily_pos"
        ),
        sa.CheckConstraint(
            "max_concurrency > 0", name="ck_tenant_sandbox_policy_concurrency_pos"
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index(
        "ix_tenant_sandbox_policy_tenant_id", "tenant_sandbox_policy", ["tenant_id"]
    )

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
    # Reverse order. Drop RLS first, then the index, then the table.
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_tenant_sandbox_policy_tenant_id", table_name="tenant_sandbox_policy")
    op.drop_table("tenant_sandbox_policy")
