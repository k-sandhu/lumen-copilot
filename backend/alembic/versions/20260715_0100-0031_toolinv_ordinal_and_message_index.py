"""tool_invocations ordering ordinal + message-hydration index (#397).

Two follow-ups from the #394 review on the same table (ADR-0008 §4 single-head
invariant — ``down_revision`` is the current head ``0030_toolinv_result_summary``):

* ``tool_invocations.ordinal`` — ``INTEGER NOT NULL DEFAULT 0``. ``created_at``
  carries the *transaction* timestamp on Postgres (``now()``), so every tool
  call recorded in one answer turn ties; the per-message ordinal (assigned by
  the runner's per-turn counter) is the real oldest-first ordering key the
  ``Message.tool_invocations`` contract promises. Existing rows backfill to 0 —
  their relative order within a tie stays as-approximate as it was.
* ``ix_tool_invocations_tenant_message`` on ``(tenant_id, message_id)`` — the
  history-reload hydration filters on exactly this pair; Postgres does not
  index the FK automatically, so without it every message-history fetch scans
  the tenant's trace rows.

Hand-written and reversible (backend/AGENTS.md "Data & migrations"): downgrade
drops the index and the column. Plain ``add_column`` / ``create_index`` so it
renders offline; the live apply runs against a disposable throwaway database.

Revision ID: 0031_toolinv_ordinal_and_message_index
Revises: 0030_toolinv_result_summary
Create Date: 2026-07-15 01:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031_toolinv_ordinal_and_message_index"
down_revision: str | None = "0030_toolinv_result_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_invocations",
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_tool_invocations_tenant_message",
        "tool_invocations",
        ["tenant_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_tenant_message", table_name="tool_invocations")
    op.drop_column("tool_invocations", "ordinal")
