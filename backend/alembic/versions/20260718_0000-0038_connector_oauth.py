"""connector_oauth — managed-connector OAuth + identity attestation (#452)

ADR-0019 §1/§2 (F-CB-1):

* ``sources`` gains the managed-OAuth surface: ``auth_secret_ref`` (FK →
  ``secrets.id``, SET NULL — the vault row holding the refresh token; never
  token material), ``connect_generation`` (the monotonic per-source flow
  counter the callback compares — a superseded flow is rejected),
  ``connected_account`` (provider-account metadata, ``{"email": ...}``), and
  ``sync_cursor`` (the ADR-0019 §3 incremental-sync resume point).
* ``users`` gains the identity attestation for connector-ACL mapping:
  ``email_attested_at`` / ``email_attested_by`` (NULL = unattested — the ACL
  mapper emits no principal for the user; any future email-mutation path must
  clear both in the same write).
* ``ck_secrets_kind`` is widened for ``connector_oauth`` — the Python enum
  alone would fail at the database boundary.

No new tables; the RLS backstop on ``sources``/``users``/``secrets`` (0007)
covers the new columns. Reversible (backend/AGENTS.md): ``downgrade`` restores
the narrow CHECK and drops the columns.

Revision ID: 0038_connector_oauth
Revises: 0037_session_summaries
Create Date: 2026-07-18 17:00:00+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0038_connector_oauth"
down_revision: str | None = "0037_session_summaries"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

_KINDS_OLD = "kind in ('mcp_auth', 'search_api', 'other')"
_KINDS_NEW = "kind in ('mcp_auth', 'search_api', 'connector_oauth', 'other')"


def upgrade() -> None:
    # --- sources: managed-OAuth surface (ADR-0019 §1/§3) --------------------
    op.add_column(
        "sources",
        sa.Column("auth_secret_ref", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sources_auth_secret_ref_secrets",
        "sources",
        "secrets",
        ["auth_secret_ref"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "sources",
        sa.Column(
            "connect_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("sources", sa.Column("connected_account", _JSON, nullable=True))
    op.add_column("sources", sa.Column("sync_cursor", sa.Text(), nullable=True))

    # --- users: identity attestation (ADR-0019 §2) --------------------------
    op.add_column(
        "users",
        sa.Column("email_attested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("email_attested_by", sa.Uuid(as_uuid=True), nullable=True),
    )

    # --- secrets: widen the kind domain (ADR-0019 §1) -----------------------
    op.drop_constraint("ck_secrets_kind", "secrets", type_="check")
    op.create_check_constraint("ck_secrets_kind", "secrets", _KINDS_NEW)


def downgrade() -> None:
    # Narrowing the CHECK requires no connector_oauth rows to remain; a real
    # downgrade deletes those vault rows first (they are worthless without the
    # sources columns being dropped alongside).
    op.execute("DELETE FROM secrets WHERE kind = 'connector_oauth'")
    op.drop_constraint("ck_secrets_kind", "secrets", type_="check")
    op.create_check_constraint("ck_secrets_kind", "secrets", _KINDS_OLD)

    op.drop_column("users", "email_attested_by")
    op.drop_column("users", "email_attested_at")

    op.drop_column("sources", "sync_cursor")
    op.drop_column("sources", "connected_account")
    op.drop_column("sources", "connect_generation")
    op.drop_constraint("fk_sources_auth_secret_ref_secrets", "sources", type_="foreignkey")
    op.drop_column("sources", "auth_secret_ref")
