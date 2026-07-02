"""assistant core — assistants + immutable assistant_versions + chat FKs (#211)

The persistence half of the assistants epic (issue #211, ADR-0011 §1): a saved,
named, governed chat configuration (instructions + model default + knowledge
scope + tool allow-list + autonomy level + owner/backup) and its immutable,
append-only published version history. ``down_revision`` is the current head
``0015_secrets`` (single-head invariant, ADR-0008 §4).

Two new tenant/owner-scoped tables:

* ``assistants`` — the **mutable head** / working definition. Non-null
  ``tenant_id`` (INV-1) + ``owner_id`` (the accountable owner, E6-8); a nullable
  ``backup_owner_id`` (required only *before publish*, §4); ``name`` /
  ``description`` / ``instructions``; a nullable ``model`` (NULL ⇒ the smart
  server default resolved at run time); ``knowledge_scope`` jsonb (a *narrowing*
  retrieval filter, §2); ``tool_allowlist`` jsonb (the CC-A allowed-tool set);
  ``autonomy_level`` + ``status`` enums (CHECK-pinned domains). Both enum domains
  are pinned at the DB so a bad value can never be stored.
* ``assistant_versions`` — **immutable** published snapshots (E6-7). Non-null
  ``tenant_id`` (INV-1) + ``assistant_id`` FK (CASCADE), a monotonic ``version``
  (UNIQUE within an assistant), the publishing ``author_id``, the **full frozen**
  ``config`` jsonb, and optional ``notes`` / ``diff_summary``. Write-once: the app
  DB role is granted no UPDATE/DELETE on it (same posture as ``audit_events``,
  spec 0004 §2.4) so a published version is a durable, auditable snapshot.

``chat_sessions`` gains two **nullable** FKs (additive; ad-hoc sessions leave
both NULL and behave exactly as today): ``assistant_id`` (ON DELETE SET NULL) and
``assistant_version_id`` (ON DELETE SET NULL) — the pinned snapshot a session ran,
so a transcript is reproducible after the assistant is edited or rolled back.

RLS: both new tables are tenant-scoped, so this migration ``ENABLE`` + ``FORCE``
row-level security on each with the same fail-closed ``app.tenant_id`` GUC policy
the ``0007`` backstop uses (spec 0004 §2.1) — additive, never relaxing an existing
policy.

Reversible (backend/AGENTS.md): ``downgrade`` re-grants the version write perms,
drops the policies, disables RLS, drops the two ``chat_sessions`` FK columns, and
drops the tables in FK order, restoring the pre-#211 state. Offline DDL render
asserts the shape; the live apply runs against a disposable throwaway database
(the #70 lesson).

Revision ID: 0016_assistants
Revises: 0015_secrets
Create Date: 2026-07-02 03:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0016_assistants"
down_revision: str | None = "0015_secrets"
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

# The two new tenant-scoped tables that get the RLS backstop.
_SCOPED_TABLES: tuple[str, ...] = ("assistants", "assistant_versions")


def upgrade() -> None:
    # --- assistants (the mutable head) --------------------------------------
    op.create_table(
        "assistants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The accountable owner (E6-8). SET NULL on user delete so the assistant
        # is not cascade-deleted — an orphaned assistant is admin-reassigned or
        # disabled, never left runnable without an owner (ADR-0011 §4).
        sa.Column(
            "owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=False,
        ),
        # Required before publish (§4), not at draft time — nullable here.
        sa.Column(
            "backup_owner_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # The system-prompt text injected at run time (persona/role).
        sa.Column("instructions", sa.Text(), nullable=True),
        # NULL ⇒ the smart server default resolved fail-closed at run time.
        sa.Column("model", sa.String(length=255), nullable=True),
        # The narrowing retrieval filter: {collection_ids, source_ids, modes}.
        sa.Column("knowledge_scope", _JSON, nullable=False),
        # The CC-A allowed-tool set (string[]). [] ⇒ the ad-hoc default set.
        sa.Column("tool_allowlist", _JSON, nullable=False),
        sa.Column("autonomy_level", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
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
        # Pin the enum domains at the DB so a bad value can never be stored.
        sa.CheckConstraint(
            "autonomy_level in ('suggest', 'draft', 'act_with_approval', 'act_auto')",
            name="ck_assistants_autonomy_level",
        ),
        sa.CheckConstraint(
            "status in ('draft', 'published', 'disabled')",
            name="ck_assistants_status",
        ),
    )
    # Tenant-leading index (INV-1) — also the parent FK column.
    op.create_index("ix_assistants_tenant_id", "assistants", ["tenant_id"])
    op.create_index("ix_assistants_owner_id", "assistants", ["owner_id"])
    # The "this owner's assistants" list path (owner-scoped, newest-first).
    op.create_index("ix_assistants_tenant_owner", "assistants", ["tenant_id", "owner_id"])

    # --- assistant_versions (immutable, append-only) ------------------------
    op.create_table(
        "assistant_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Monotonic per assistant; UNIQUE within the assistant.
        sa.Column("version", sa.Integer(), nullable=False),
        # Who published (or rolled back to create) this version. SET NULL so
        # removing that user does not cascade-delete the immutable snapshot.
        sa.Column(
            "author_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The full frozen definition captured at publish/rollback time.
        sa.Column("config", _JSON, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("diff_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "assistant_id", "version", name="uq_assistant_versions_assistant_version"
        ),
        sa.CheckConstraint("version >= 1", name="ck_assistant_versions_version_positive"),
    )
    op.create_index("ix_assistant_versions_tenant_id", "assistant_versions", ["tenant_id"])
    op.create_index(
        "ix_assistant_versions_tenant_assistant",
        "assistant_versions",
        ["tenant_id", "assistant_id"],
    )

    # Write-once (append-only): the app DB role holds INSERT/SELECT but NO
    # UPDATE/DELETE on the version history — a published version is a durable,
    # auditable snapshot (same posture as audit_events, spec 0004 §2.4).
    if op.get_context().dialect.name == "postgresql":
        op.execute("REVOKE UPDATE, DELETE ON TABLE assistant_versions FROM CURRENT_USER")

    # --- chat_sessions gains two nullable pin FKs (additive) ----------------
    op.add_column(
        "chat_sessions",
        sa.Column(
            "assistant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistants.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "assistant_version_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("assistant_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_chat_sessions_assistant_id", "chat_sessions", ["assistant_id"]
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
    # Reverse order. Drop RLS first, then the chat FKs, then the tables (children
    # → parents), re-granting the immutable-version write perms before the drop so
    # the revoke does not strand a managed role.
    if op.get_context().dialect.name == "postgresql":
        for table in _SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS rls_{table} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_chat_sessions_assistant_id", table_name="chat_sessions")
    op.drop_column("chat_sessions", "assistant_version_id")
    op.drop_column("chat_sessions", "assistant_id")

    if op.get_context().dialect.name == "postgresql":
        op.execute("GRANT UPDATE, DELETE ON TABLE assistant_versions TO CURRENT_USER")
    op.drop_index("ix_assistant_versions_tenant_assistant", table_name="assistant_versions")
    op.drop_index("ix_assistant_versions_tenant_id", table_name="assistant_versions")
    op.drop_table("assistant_versions")

    op.drop_index("ix_assistants_tenant_owner", table_name="assistants")
    op.drop_index("ix_assistants_owner_id", table_name="assistants")
    op.drop_index("ix_assistants_tenant_id", table_name="assistants")
    op.drop_table("assistants")
