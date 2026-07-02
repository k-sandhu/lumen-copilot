"""artifact store — agent/run-produced files, tenant/owner-scoped + RLS (#208)

The persistence half of the **artifact store** (CC-12): files an agent/run
*produces* (distinct from user-uploaded ``documents``) — the seam the
file-writing tool and the code sandbox persist their output through. Mirrors the
``documents`` tenant/owner + storage-key pattern, but the row is **immutable**
(no ``updated_at``, no ``status``: the bytes exist the moment the row does; a new
version is a new row). ``down_revision`` is the current head
``0012_tenant_max_tool_turns`` (single-head invariant, ADR-0008 §4).

Shape (issue #208 §1):

* ``id`` (uuid PK), ``tenant_id`` FK → ``tenants`` (non-null, indexed — every
  artifact is tenant-scoped, INV-1), ``owner_id`` FK → ``users`` (ownership-
  bearing, spec 0004 §2.2 — a user sees only their own);
* ``produced_by`` ∈ {``chat_session``, ``run``, ``tool``} — the write origin;
* ``session_id`` FK → ``chat_sessions`` (nullable, CASCADE) — the only real FK of
  the three back-links because its table exists; ``run_id`` /
  ``tool_invocation_id`` are plain nullable UUID columns (their referent tables —
  the run/agent framework, CC-A — do not exist yet, mirroring how ``grants`` keeps
  ``resource_id`` FK-less; they become FKs when those tables land);
* ``filename``, ``mime_type``, ``size_bytes`` (>= 0), ``storage_key``
  (tenant-prefixed, content-addressed under the ``artifacts/`` prefix), ``sha256``
  (the content address);
* ``retention_expires_at`` (nullable ⇒ keep forever) — the janitor purge boundary;
* ``created_at`` (server-defaulted).

Constraints/indexes:

* ``ck_artifacts_size_nonneg`` and ``ck_artifacts_produced_by`` pin the size and
  the ``produced_by`` enum domain at the DB so a bad value can never be stored;
* ``ix_artifacts_tenant_owner`` (tenant_id, owner_id) — the list-my-artifacts hot
  path (tenant-leading, INV-1);
* ``ix_artifacts_owner_id`` (owner_id) — the ownership FK column;
* ``ix_artifacts_session_id`` (session_id) — list a session's artifacts;
* ``ix_artifacts_retention`` — the retention janitor's sweep, a **partial** index
  (``WHERE retention_expires_at IS NOT NULL``) so the common keep-forever rows do
  not bloat it.

RLS: ``artifacts`` is tenant-scoped, so this migration also ``ENABLE`` + ``FORCE``
row-level security on it with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses for every other tenant-scoped table (spec 0004 §2.1,
#17) — additive, never relaxing an existing policy.

Reversible (backend/AGENTS.md): ``downgrade`` drops the policy, disables RLS, and
drops the table, restoring the pre-#208 state. Offline DDL render asserts the
shape; the live apply runs against a disposable throwaway database (#70 lesson).

Revision ID: 0014_artifacts
Revises: 0012_tenant_max_tool_turns
Create Date: 2026-07-02 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_artifacts"
down_revision: str | None = "0013_tool_invocations"
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
        "artifacts",
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
        # The write origin. Modelled as a string + a CHECK (not a native enum) so
        # the domain can grow without an ALTER TYPE, matching the grants pattern.
        sa.Column("produced_by", sa.String(length=20), nullable=False),
        # Optional back-links to the producing context. Only session_id is an FK
        # (its table exists, CASCADE like the other links); run_id /
        # tool_invocation_id are plain UUIDs until the run/tool framework (CC-A).
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tool_invocation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # Tenant-prefixed, content-addressed object key under the artifacts/ prefix.
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        # NULL ⇒ keep forever; a timestamp is the retention-janitor purge boundary.
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonneg"),
        sa.CheckConstraint(
            "produced_by in ('chat_session', 'run', 'tool')",
            name="ck_artifacts_produced_by",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_artifacts_tenant_id", "artifacts", ["tenant_id"])
    # The ownership FK column.
    op.create_index("ix_artifacts_owner_id", "artifacts", ["owner_id"])
    # The list-my-artifacts hot path (tenant + owner).
    op.create_index("ix_artifacts_tenant_owner", "artifacts", ["tenant_id", "owner_id"])
    # List a session's produced artifacts.
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"])
    # The retention janitor's sweep — partial on Postgres so keep-forever rows
    # (retention_expires_at IS NULL) don't bloat it. Plain index elsewhere.
    if op.get_context().dialect.name == "postgresql":
        op.create_index(
            "ix_artifacts_retention",
            "artifacts",
            ["retention_expires_at"],
            postgresql_where=sa.text("retention_expires_at IS NOT NULL"),
        )
    else:
        op.create_index("ix_artifacts_retention", "artifacts", ["retention_expires_at"])

    # RLS backstop, consistent with the 0007 per-table policy (spec 0004 §2.1).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE artifacts FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY rls_artifacts ON artifacts "
            f"USING ({_PREDICATE}) "
            f"WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS rls_artifacts ON artifacts")
        op.execute("ALTER TABLE artifacts NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE artifacts DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_artifacts_retention", table_name="artifacts")
    op.drop_index("ix_artifacts_session_id", table_name="artifacts")
    op.drop_index("ix_artifacts_tenant_owner", table_name="artifacts")
    op.drop_index("ix_artifacts_owner_id", table_name="artifacts")
    op.drop_index("ix_artifacts_tenant_id", table_name="artifacts")
    op.drop_table("artifacts")
