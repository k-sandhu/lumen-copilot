"""encrypted per-tenant secrets vault — tenant/owner-scoped credentials (#209)

The persistence half of the secrets vault (issue #209): an **encrypted,
per-tenant** store for third-party credentials the platform holds on a user's
behalf (MCP auth tokens/headers, a hosted web-search key, future connector
credentials). Credential handling is a single-chokepoint concern; this table is
the credential seam for E3 (MCP) and SPIKE-4. ``down_revision`` is the current
head ``0012_tenant_max_tool_turns`` (single-head invariant, ADR-0008 §4).

**No column ever holds plaintext.** A credential lives only as ``ciphertext`` +
``nonce`` under ``key_version`` (AES-256-GCM envelope encryption,
``app.core.crypto``); ``hint`` is a non-reversing masked tail for the UI. The
cipher is applied in ``services/secrets_service.py`` — the sole caller — never at
the DB.

Shape (issue #209 §1):

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed — every
  secret is tenant-scoped, INV-1), ``owner_id`` FK → ``users`` (the owning
  principal; "user sees only their own" default, §2.2);
* ``name`` + ``kind`` (enum ``mcp_auth|search_api|other``, ``CheckConstraint``-
  pinned) — the handle + what the secret is for;
* ``ciphertext`` (``bytea``) + ``nonce`` (``bytea``) + ``key_version`` (int,
  rotation-ready, ``>= 1``) — the envelope, never the plaintext;
* ``hint`` (text) — a masked tail (e.g. last 4 chars), not the value;
* ``created_by`` FK → ``users`` (who stored it; ``SET NULL`` on user delete) and
  ``created_at`` / ``updated_at``.

Constraints/indexes:

* a UNIQUE on ``(tenant_id, owner_id, name)`` so a secret name is a per-owner
  singleton — re-storing rotates in place (a stable handle for an adapter), never
  a duplicate;
* an index on ``(tenant_id, owner_id)`` — the "this owner's secrets" list/lookup
  path.

RLS: ``secrets`` is tenant-scoped, so this migration also ``ENABLE`` + ``FORCE``
row-level security on it with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1) —
additive, never relaxing an existing policy. (External-KMS integration is a
future seam — issue #209 scope fence "OUT"; the ciphertext columns are unchanged
by that swap.)

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#209 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (#70 lesson).

Revision ID: 0015_secrets
Revises: 0012_tenant_max_tool_turns
Create Date: 2026-07-02 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_secrets"
down_revision: str | None = "0014_artifacts"
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


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        # The envelope: encrypted bytes (with GCM's auth tag) + the per-encryption
        # nonce + the key version that produced it. NEVER the plaintext (#209).
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        # A non-reversing masked tail for the UI (e.g. last 4 chars). Not the value.
        sa.Column("hint", sa.String(length=64), nullable=False),
        # Who stored it (owner or a tenant admin). SET NULL so removing that user
        # does not cascade-delete the secret.
        sa.Column(
            "created_by",
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
        # A secret name is a per-owner singleton — re-storing rotates in place.
        sa.UniqueConstraint("tenant_id", "owner_id", "name", name="uq_secrets_owner_name"),
        sa.CheckConstraint(
            "kind in ('mcp_auth', 'search_api', 'other')",
            name="ck_secrets_kind",
        ),
        sa.CheckConstraint("key_version >= 1", name="ck_secrets_key_version_positive"),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_secrets_tenant_id", "secrets", ["tenant_id"])
    op.create_index("ix_secrets_owner_id", "secrets", ["owner_id"])
    # The "this owner's secrets" list/lookup path.
    op.create_index("ix_secrets_tenant_owner", "secrets", ["tenant_id", "owner_id"])

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE secrets ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE secrets FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_secrets ON secrets "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_secrets ON secrets")
        op.execute("ALTER TABLE secrets NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE secrets DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_secrets_tenant_owner", table_name="secrets")
    op.drop_index("ix_secrets_owner_id", table_name="secrets")
    op.drop_index("ix_secrets_tenant_id", table_name="secrets")
    op.drop_table("secrets")
